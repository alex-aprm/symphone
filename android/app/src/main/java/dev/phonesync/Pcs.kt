package dev.phonesync

import android.content.Context
import android.content.SharedPreferences
import android.util.Log
import org.json.JSONArray
import org.json.JSONObject
import java.net.Inet4Address
import java.net.InetAddress
import java.security.SecureRandom
import java.util.concurrent.CopyOnWriteArrayList

/** Credentials of a PC-hosted Wi-Fi Direct legacy network. */
data class Wfd(val ssid: String, val pass: String) {
    fun json(): JSONObject = JSONObject().put("ssid", ssid).put("pass", pass)
}

/** One paired computer. Mutable fields are only changed through [Pcs] (which persists them). */
class Pc(
    @Volatile var id: String,
    @Volatile var name: String,
    val token: String,
    @Volatile var master: Boolean = false,
    @Volatile var lastSeen: Long = 0,
    @Volatile var lastIp: String = "",
    @Volatile var wfd: Wfd? = null,
    /** The PC's peer port (/peer/hello); 47801 unless its pairing QR said otherwise (p=). */
    @Volatile var peerPort: Int = Hub.PC_PEER_PORT,
) {
    /** Last plain request (not counting event-stream keep-alives); not persisted. */
    @Volatile var lastRequest: Long = 0

    /** Live = it holds an SSE subscription, or made a request very recently. */
    val online: Boolean get() = Hub.hasSubscriber(id) || System.currentTimeMillis() - lastRequest < 15_000

    fun json(): JSONObject = JSONObject()
        .put("id", id).put("name", name).put("token", token).put("master", master)
        .put("lastSeen", lastSeen).put("lastIp", lastIp).put("wfd", wfd?.json() ?: JSONObject.NULL).put("peerPort", peerPort)

    /** Public view (no secrets). */
    fun publicJson(): JSONObject = JSONObject().put("id", id).put("name", name).put("online", online).put("master", master)

    companion object {
        fun from(o: JSONObject) = Pc(
            o.getString("id"), o.optString("name", "PC"), o.getString("token"), o.optBoolean("master"),
            o.optLong("lastSeen"), o.optString("lastIp"),
            o.optJSONObject("wfd")?.let { w -> w.optString("ssid").takeIf { it.isNotEmpty() }?.let { Wfd(it, w.optString("pass")) } },
            o.optInt("peerPort", Hub.PC_PEER_PORT),
        )
    }
}

/**
 * Paired PCs: one token per PC, exactly one master. Persisted as JSON in SharedPreferences ("pcs").
 * The v1 global token is migrated to an entry with id "legacy" until that PC registers its real id.
 */
object Pcs {
    const val LEGACY = "legacy"
    private const val TAG = "SymphonePcs"
    private val rnd = SecureRandom()
    private lateinit var prefs: SharedPreferences
    private val list = CopyOnWriteArrayList<Pc>()
    private val listeners = CopyOnWriteArrayList<() -> Unit>()
    @Volatile private var lastSave = 0L

    fun init(ctx: Context) {
        prefs = ctx.getSharedPreferences("phonesync", Context.MODE_PRIVATE)
        val raw = prefs.getString("pcs", null)
        if (raw != null) {
            try {
                val a = JSONArray(raw)
                for (i in 0 until a.length()) list += Pc.from(a.getJSONObject(i))
            } catch (e: Exception) { Log.w(TAG, "bad pcs json: $e") }
        } else {
            // Migration from v1: the single global token becomes the "legacy" PC.
            prefs.getString("token", null)?.let { tok ->
                list += Pc(LEGACY, "PC", tok, master = true)
                Log.i(TAG, "migrated legacy token")
            }
            save(notify = false)
        }
        fixMaster()
    }

    // ---------------------------------------------------------------- queries
    fun all(): List<Pc> = list.toList()
    val isEmpty get() = list.isEmpty()
    fun byId(id: String?): Pc? = if (id.isNullOrEmpty()) null else list.firstOrNull { it.id == id }
    fun byToken(tok: String?): Pc? {
        if (tok.isNullOrEmpty()) return null
        var hit: Pc? = null
        for (p in list) if (Http.constEq(tok, p.token)) hit = p // no early exit: constant-ish time
        return hit
    }
    val master: Pc? get() = list.firstOrNull { it.master } ?: list.firstOrNull()

    fun publicList(): JSONArray = JSONArray().also { a -> list.forEach { a.put(it.publicJson()) } }
    fun eventJson(): JSONObject = JSONObject().put("master", master?.id ?: "").put("pcs", publicList())

    // ---------------------------------------------------------------- changes
    /** Returns the entry for [id], creating it (with a fresh token) if needed. The first PC becomes master. */
    @Synchronized
    fun getOrCreate(id: String, name: String?): Pc {
        val cleanId = id.trim().take(64).ifEmpty { LEGACY }
        byId(cleanId)?.let { p ->
            if (!name.isNullOrBlank() && name != p.name) { p.name = name.trim().take(80); changed() }
            return p
        }
        val p = Pc(cleanId, name?.trim()?.take(80)?.ifEmpty { null } ?: defaultName(cleanId), newToken(), master = list.isEmpty())
        list += p
        Log.i(TAG, "new pc ${p.id} (${p.name})")
        changed()
        return p
    }

    /** The legacy (v1) token; creates the entry on first use (v1 PCs pairing over adb / code without a pc object). */
    fun legacy(): Pc = getOrCreate(LEGACY, null)

    /** POST /api/pc from [pc]: register/rename. A legacy entry takes its real id (merging any duplicate entry). */
    @Synchronized
    fun register(pc: Pc, id: String?, name: String?) {
        val newId = id?.trim()?.take(64).orEmpty()
        if (newId.isNotEmpty() && newId != pc.id && pc.id == LEGACY) {
            byId(newId)?.let { dup ->
                // The requester proves itself with the legacy token: keep this entry, drop the duplicate.
                if (dup.master) pc.master = true
                if (pc.wfd == null) pc.wfd = dup.wfd
                if (pc.lastIp.isEmpty()) pc.lastIp = dup.lastIp
                list.remove(dup)
                Hub.retag(dup.id, null)
            }
            Hub.retag(pc.id, newId)
            pc.id = newId
        } else if (newId.isNotEmpty() && newId != pc.id && byId(newId) == null) {
            Hub.retag(pc.id, newId)
            pc.id = newId
        }
        if (!name.isNullOrBlank()) pc.name = name.trim().take(80)
        if (pc.name == "PC" || pc.name.isEmpty()) pc.name = defaultName(pc.id)
        fixMaster()
        changed()
    }

    @Synchronized
    fun setMaster(id: String): Boolean {
        val p = byId(id) ?: return false
        for (o in list) o.master = o === p
        changed()
        return true
    }

    @Synchronized
    fun remove(id: String): Boolean {
        val p = byId(id) ?: return false
        list.remove(p)
        fixMaster()
        changed()
        Hub.dropSubscribers(p.id)
        return true
    }

    @Synchronized
    fun setWfd(pc: Pc, wfd: Wfd?) { pc.wfd = wfd; changed() }

    /** Where the PC answered during QR pairing. */
    @Synchronized
    fun setPeer(pc: Pc, ip: String?, port: Int) {
        if (!ip.isNullOrEmpty()) pc.lastIp = ip
        pc.peerPort = port
        changed()
    }

    /** Called for every authenticated request: remember when and from where we last heard of [pc]. */
    fun seen(pc: Pc, remote: InetAddress?, request: Boolean = true) {
        val now = System.currentTimeMillis()
        pc.lastSeen = now
        if (request) pc.lastRequest = now
        var ipChanged = false
        if (remote is Inet4Address && !remote.isLoopbackAddress && !remote.isAnyLocalAddress && !remote.isLinkLocalAddress) {
            val ip = remote.hostAddress ?: ""
            if (ip.isNotEmpty() && ip != pc.lastIp) { pc.lastIp = ip; ipChanged = true }
        }
        if (ipChanged || now - lastSave > 60_000) save(notify = false)
    }

    fun addListener(l: () -> Unit) { listeners += l }
    fun removeListener(l: () -> Unit) { listeners -= l }

    /** Something visible changed (list, names, master, online state): persist + notify UI / SSE / shortcuts. */
    fun changed() {
        save(notify = true)
    }

    /** Only online state changed (SSE subscribe/unsubscribe). */
    fun onlineChanged() {
        Hub.emitAll("pcs", eventJson())
        for (l in listeners) try { l() } catch (e: Exception) { Log.w(TAG, "listener: $e") }
    }

    private fun fixMaster() {
        if (list.isEmpty()) return
        val masters = list.filter { it.master }
        if (masters.size != 1) {
            val keep = masters.firstOrNull() ?: list.first()
            for (o in list) o.master = o === keep
        }
    }

    @Synchronized
    private fun save(notify: Boolean) {
        lastSave = System.currentTimeMillis()
        val a = JSONArray()
        list.forEach { a.put(it.json()) }
        prefs.edit().putString("pcs", a.toString()).apply()
        if (notify) {
            Hub.emitAll("pcs", eventJson())
            for (l in listeners) try { l() } catch (e: Exception) { Log.w(TAG, "listener: $e") }
        }
    }

    private fun defaultName(id: String) = if (id == LEGACY) "PC" else "PC ${id.take(4)}"

    fun newToken(): String {
        val b = ByteArray(16); rnd.nextBytes(b)
        return b.joinToString("") { "%02x".format(it) }
    }
}

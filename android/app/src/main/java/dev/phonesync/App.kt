package dev.phonesync

import android.app.Application
import android.content.Context
import android.content.Intent
import android.content.SharedPreferences
import android.os.Build
import android.provider.Settings
import org.json.JSONObject
import java.security.SecureRandom
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.LinkedBlockingQueue

class App : Application() {
    override fun onCreate() {
        super.onCreate()
        Hub.init(this)
    }
}

/** Process-wide state shared by the server, listeners and activities. */
object Hub {
    const val HTTP_PORT = 47900
    const val UDP_PORT = 47800
    const val PC_PEER_PORT = 47801

    private val rnd = SecureRandom()

    lateinit var ctx: Context
        private set
    lateinit var prefs: SharedPreferences
        private set

    /** Short code shown in the app for pairing a PC over Wi-Fi without USB. Rotates on demand. */
    @Volatile var pairCode: String = randomDigits(6)
        private set

    fun rotatePairCode() { pairCode = randomDigits(6) }

    val deviceId: String
        get() = prefs.getString("id", null) ?: Pcs.newToken().take(12).also { prefs.edit().putString("id", it).apply() }

    val deviceName: String
        get() = Settings.Global.getString(ctx.contentResolver, Settings.Global.DEVICE_NAME)
            ?: "${Build.MANUFACTURER} ${Build.MODEL}"

    /** "Share clipboard with PC" toggle (default on). */
    var clipShare: Boolean
        get() = prefs.getBoolean("clipShare", true)
        set(v) { prefs.edit().putBoolean("clipShare", v).apply() }

    fun init(context: Context) {
        ctx = context.applicationContext
        prefs = ctx.getSharedPreferences("phonesync", Context.MODE_PRIVATE)
        Pcs.init(ctx)
        try {
            @Suppress("DEPRECATION")
            ctx.registerReceiver(object : android.content.BroadcastReceiver() {
                override fun onReceive(c: Context, i: Intent) = Wallpaper.invalidate()
            }, android.content.IntentFilter(Intent.ACTION_WALLPAPER_CHANGED), Context.RECEIVER_NOT_EXPORTED)
        } catch (_: Exception) {}
    }

    fun startService(context: Context) {
        context.startForegroundService(Intent(context, SyncService::class.java))
    }

    fun pingJson(): JSONObject = JSONObject()
        .put("app", "phonesync")
        .put("version", 1)
        .put("v", 2)
        .put("id", deviceId)
        .put("name", deviceName)
        .put("model", Build.MODEL)
        .put("manufacturer", Build.MANUFACTURER)
        .put("android", Build.VERSION.RELEASE)
        .put("port", HTTP_PORT)
        .put("ble", Ble.advertising)

    // ---- Server-sent events fan-out (each subscriber is tagged with the PC that opened it) ----

    class Subscriber(@Volatile var pc: String?) {
        val queue = LinkedBlockingQueue<String>(10_000)
        @Volatile var closed = false
    }

    private val subscribers = CopyOnWriteArrayList<Subscriber>()

    fun subscribe(pc: String?): Subscriber = Subscriber(pc).also { subscribers.add(it); Pcs.onlineChanged() }
    fun unsubscribe(s: Subscriber) { if (subscribers.remove(s)) Pcs.onlineChanged() }
    val subscriberCount get() = subscribers.size
    fun hasSubscriber(pc: String?): Boolean = pc != null && subscribers.any { it.pc == pc && !it.closed }

    /** A PC changed its id (legacy -> real id) or was removed (newId == null). */
    fun retag(oldId: String, newId: String?) {
        for (s in subscribers) if (s.pc == oldId) {
            if (newId != null) s.pc = newId else { s.closed = true; s.queue.offer("") }
        }
    }
    fun dropSubscribers(pc: String) = retag(pc, null)

    private fun format(event: String, data: JSONObject) =
        "event: $event\ndata: ${data.toString().replace("\n", "\\n")}\n\n"

    /** To every connected PC. */
    fun emit(event: String, data: JSONObject) = emitAll(event, data)
    fun emitAll(event: String, data: JSONObject) {
        if (subscribers.isEmpty()) return
        val msg = format(event, data)
        for (s in subscribers) s.queue.offer(msg)
    }

    /** To one PC only. Returns false if it has no live subscriber. */
    fun emitTo(pc: String, event: String, data: JSONObject): Boolean {
        val msg = format(event, data)
        var any = false
        for (s in subscribers) if (s.pc == pc && !s.closed) { s.queue.offer(msg); any = true }
        return any
    }

    /**
     * Targeted event (share, share_text, clip): to [target] (null = the master PC). Anything meant for the master
     * goes to every PC while the master has no live subscriber, so a single connected PC never misses it.
     */
    fun emitTargeted(event: String, data: JSONObject, target: String?) {
        val masterId = Pcs.master?.id
        val t = target?.takeIf { Pcs.byId(it) != null } ?: masterId
        if (t == null) { emitAll(event, data); return }
        if (emitTo(t, event, data)) return
        if (t == masterId) emitAll(event, data)
    }

    // ---- Clipboard echo suppression ----
    @Volatile var lastClipFromPc: String? = null
    @Volatile private var lastClipSent: String? = null
    @Volatile private var lastClipSentAt = 0L

    /** Phone clipboard changed (app listener or shell helper): tell the master PC, unless it came from a PC. */
    fun phoneClipChanged(text: String?, source: String): Boolean {
        if (text.isNullOrEmpty() || !clipShare) return false
        if (text == lastClipFromPc) return false
        val now = System.currentTimeMillis()
        if (text == lastClipSent && now - lastClipSentAt < 3000) return false // both sources saw the same change
        lastClipSent = text; lastClipSentAt = now
        emitTargeted("clip", JSONObject().put("text", text).put("source", source), null)
        return true
    }

    private fun randomDigits(n: Int) = (1..n).map { rnd.nextInt(10) }.joinToString("")
}

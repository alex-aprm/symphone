package dev.phonesync

import android.net.Network
import android.net.Uri
import android.os.Build
import android.util.Log
import org.json.JSONObject
import java.net.URL
import java.util.concurrent.ExecutorCompletionService
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

/**
 * QR pairing (protocol v2):
 * `phonesync://pair?v=2&pc=<id>&n=<name>&s=<secret>&a=<ip1>,<ip2>&p=47801[&b=1][&w=<ssid>&k=<pass>]`
 * 1. POST /peer/pair to every address in `a` (3 s timeout each), first success wins;
 * 2. else join the PC's Wi-Fi Direct network (w/k) and retry on 192.168.137.1;
 * 3. else BLE pairing (b=1).
 */
object Pairing {
    private const val TAG = "SymphonePair"

    class Request(
        val pcId: String, val pcName: String, val secret: String, val addrs: List<String>, val port: Int,
        val ble: Boolean, val wfd: Wfd?,
    )

    fun parse(uri: Uri?): Request? {
        if (uri == null || uri.scheme != "phonesync" || uri.host != "pair") return null
        val pc = uri.getQueryParameter("pc")?.trim().orEmpty()
        val s = uri.getQueryParameter("s")?.trim().orEmpty()
        if (pc.isEmpty() || s.length < 16) return null
        val addrs = uri.getQueryParameter("a").orEmpty().split(',').map { it.trim() }
            .filter { it.isNotEmpty() && it.matches(Regex("[0-9A-Za-z.:\\-\\[\\]]+")) }
        val w = uri.getQueryParameter("w")
        val k = uri.getQueryParameter("k")
        return Request(
            pc, uri.getQueryParameter("n")?.takeIf { it.isNotBlank() } ?: "PC", s, addrs,
            uri.getQueryParameter("p")?.toIntOrNull() ?: Hub.PC_PEER_PORT,
            uri.getQueryParameter("b") == "1",
            if (!w.isNullOrEmpty() && k != null) Wfd(w, k) else null,
        )
    }

    fun parse(text: String): Request? = try { parse(Uri.parse(text.trim())) } catch (_: Exception) { null }

    private fun phoneJson(token: String, ip: String) = JSONObject()
        .put("id", Hub.deviceId).put("name", Hub.deviceName).put("model", Build.MODEL)
        .put("token", token).put("ip", ip).put("port", Hub.HTTP_PORT)

    /** POST /peer/pair; returns the PC's {"id","name"} (plus "_host") on success. */
    private fun post(host: String, r: Request, token: String, ip: String, net: Network?): JSONObject? {
        val h = if (host.contains(':') && !host.startsWith("[")) "[$host]" else host
        return try {
            val body = JSONObject().put("s", r.secret).put("phone", phoneJson(token, ip))
            val (code, text) = Arrival.postJson(URL("http://$h:${r.port}/peer/pair"), body, net, 3000)
            Log.i(TAG, "peer/pair $host -> $code")
            if (code != 200) return null
            val o = JSONObject(text)
            if (!o.optBoolean("ok")) null else (o.optJSONObject("pc") ?: JSONObject()).put("_host", host)
        } catch (e: Exception) {
            Log.i(TAG, "peer/pair $host: $e")
            null
        }
    }

    /**
     * Runs the whole flow (blocking; call off the main thread). [progress] gets human-readable steps.
     * Returns the paired PC or null.
     */
    fun run(r: Request, progress: (String) -> Unit): Pc? {
        val existed = Pcs.byId(r.pcId) != null
        // The PC may use the token the moment it receives it, so the entry must exist before we send it.
        val pc = Pcs.getOrCreate(r.pcId, r.pcName)
        var ok: JSONObject? = null

        // 1. LAN, all addresses in parallel.
        if (r.addrs.isNotEmpty()) {
            progress("Contacting ${r.pcName} on the network…")
            val ip = Arrival.currentIp() ?: ""
            val net = Arrival.wifiNetwork
            val pool = Executors.newFixedThreadPool(r.addrs.size.coerceAtMost(6))
            val ecs = ExecutorCompletionService<JSONObject?>(pool)
            r.addrs.forEach { a -> ecs.submit { post(a, r, pc.token, ip, net) } }
            for (i in r.addrs.indices) {
                val res = try { ecs.poll(5, TimeUnit.SECONDS)?.get() } catch (_: Exception) { null }
                if (res != null) { ok = res; break }
            }
            pool.shutdownNow()
        }

        // 2. Wi-Fi Direct.
        val oldWfd = pc.wfd
        if (ok == null && r.wfd != null) {
            progress("Joining ${r.pcName}'s direct Wi-Fi (${r.wfd.ssid})…")
            Pcs.setWfd(pc, r.wfd) // so WfdClient keeps the link while we use it
            val n = WfdClient.join(r.wfd, r.pcId)
            if (n != null) {
                val ip = WfdClient.myIp(n) ?: ""
                progress("Contacting ${r.pcName} over direct Wi-Fi…")
                for (attempt in 0 until 3) {
                    ok = post(WfdClient.HOST, r, pc.token, ip, n)
                    if (ok != null) break
                    Thread.sleep(1500)
                }
            }
        }

        // 3. Bluetooth LE: the PC comes to us.
        if (ok == null && r.ble) {
            progress("Waiting for ${r.pcName} over Bluetooth…")
            Ble.start()
            if (Ble.advertising || Ble.status == "starting") {
                val got = Ble.awaitPairing(r.secret, r.pcId, r.pcName, 90_000)
                if (got != null) ok = JSONObject().put("id", got.id).put("name", got.name)
            } else Log.i(TAG, "BLE unavailable: ${Ble.status}")
        }

        if (ok == null) {
            if (!existed) Pcs.remove(pc.id) else if (pc.wfd != oldWfd) Pcs.setWfd(pc, oldWfd)
            if (r.wfd != null) WfdClient.release("pairing failed")
            return null
        }
        val name = ok.optString("name").ifBlank { r.pcName }
        val real = Pcs.byId(r.pcId) ?: pc
        Pcs.register(real, ok.optString("id").ifBlank { null }?.takeIf { it == r.pcId }, name)
        if (r.wfd != null) Pcs.setWfd(real, r.wfd)
        // Remember where it answered (not the Wi-Fi Direct gateway: hellos there go through WfdClient).
        Pcs.setPeer(real, ok.optString("_host").takeIf { it.isNotEmpty() && it != WfdClient.HOST }, r.port)
        Log.i(TAG, "paired with ${real.name} (${real.id}), master=${real.master}")
        Arrival.burst("paired")
        return real
    }
}

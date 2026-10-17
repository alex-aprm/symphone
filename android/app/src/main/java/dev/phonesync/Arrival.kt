package dev.phonesync

import android.content.Context
import android.net.ConnectivityManager
import android.net.LinkProperties
import android.net.Network
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import android.os.Handler
import android.os.HandlerThread
import android.util.Log
import org.json.JSONObject
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.HttpURLConnection
import java.net.Inet4Address
import java.net.InetAddress
import java.net.URL
import java.util.concurrent.atomic.AtomicInteger
import javax.crypto.Mac
import javax.crypto.spec.SecretKeySpec

/**
 * "Arrival" on a Wi-Fi network: tell every paired PC where we are, right away and every 60 s while on Wi-Fi.
 * - UDP beacon `PHONESYNC!{json}` to broadcast and unicast to each PC's lastIp:47800
 * - POST http://<lastIp>:47801/peer/hello {id, ip, port, ts, sig}, sig = hex(HMAC-SHA256(pc token, id|ip|ts))
 */
object Arrival {
    private const val TAG = "SymphoneArrival"
    private const val PERIOD = 60_000L

    /** The regular Wi-Fi network (never the PC's Wi-Fi Direct network, which has no INTERNET capability). */
    @Volatile var wifiNetwork: Network? = null
        private set
    @Volatile private var wifiIp: String? = null

    /** Consecutive failed hellos to the master PC (feeds the Wi-Fi Direct fallback). */
    val masterFails = AtomicInteger()

    private val thread = HandlerThread("arrival").apply { start() }
    val handler = Handler(thread.looper)
    private var started = false
    private val cm by lazy { Hub.ctx.getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager }

    private val periodic = object : Runnable {
        override fun run() {
            if (wifiNetwork != null) burstNow("periodic")
            WfdClient.reevaluate()
            handler.postDelayed(this, PERIOD)
        }
    }

    private val callback = object : ConnectivityManager.NetworkCallback() {
        override fun onAvailable(network: Network) {
            Log.i(TAG, "wifi available $network")
            wifiNetwork = network
            // Link properties (our IP) usually arrive a moment later; burst again on change.
            handler.postDelayed({ burstNow("connect") }, 1500)
        }

        override fun onLinkPropertiesChanged(network: Network, lp: LinkProperties) {
            if (network != wifiNetwork) return
            val ip = ipv4(lp)
            if (ip != null && ip != wifiIp) {
                wifiIp = ip
                Ble.refresh()
                handler.post { burstNow("ip $ip") }
            }
        }

        override fun onLost(network: Network) {
            if (network != wifiNetwork) return
            Log.i(TAG, "wifi lost $network")
            wifiNetwork = null
            wifiIp = null
            Ble.refresh()
            handler.post { WfdClient.reevaluate() }
        }
    }

    fun start() {
        if (started) return
        started = true
        try {
            cm.registerNetworkCallback(
                NetworkRequest.Builder().addTransportType(NetworkCapabilities.TRANSPORT_WIFI)
                    .addCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET).build(),
                callback, handler)
        } catch (e: Exception) { Log.w(TAG, "callback: $e") }
        handler.postDelayed(periodic, PERIOD)
    }

    /** Current Wi-Fi IPv4 (from the tracked network, else from the interface list). */
    fun currentIp(): String? = wifiIp ?: wifiNetwork?.let { n -> cm.getLinkProperties(n)?.let { ipv4(it) } } ?: Discovery.wifiIp()

    fun ipv4(lp: LinkProperties): String? =
        lp.linkAddresses.map { it.address }.firstOrNull { it is Inet4Address && !it.isLoopbackAddress }?.hostAddress

    /** Fire a beacon + hello burst now (from any thread). */
    fun burst(reason: String) = handler.post { burstNow(reason) }

    private fun burstNow(reason: String) {
        val net = wifiNetwork ?: return
        val ip = currentIp() ?: return
        Log.i(TAG, "burst ($reason) ip=$ip")
        Discovery.beaconNow()
        val pcs = Pcs.all()
        // Unicast beacons, bound to the Wi-Fi network (the default network may be cellular).
        try {
            DatagramSocket().use { s ->
                try { net.bindSocket(s) } catch (_: Exception) {}
                val out = ("PHONESYNC!" + Hub.pingJson().put("ip", ip)).toByteArray()
                for (pc in pcs) {
                    if (pc.lastIp.isEmpty()) continue
                    try { s.send(DatagramPacket(out, out.size, InetAddress.getByName(pc.lastIp), Hub.UDP_PORT)) }
                    catch (e: Exception) { Log.d(TAG, "unicast ${pc.lastIp}: $e") }
                }
            }
        } catch (e: Exception) { Log.w(TAG, "unicast: $e") }
        for (pc in pcs) {
            if (pc.lastIp.isEmpty()) continue
            Thread({
                val ok = hello(pc, pc.lastIp, ip, net)
                if (pc.master) {
                    if (ok) { masterFails.set(0); WfdClient.lanOk() }
                    else if (masterFails.incrementAndGet() >= 3) handler.post { WfdClient.reevaluate() }
                }
            }, "hello").start()
        }
    }

    fun sign(token: String, msg: String): String {
        val mac = Mac.getInstance("HmacSHA256")
        mac.init(SecretKeySpec(token.toByteArray(), "HmacSHA256"))
        return mac.doFinal(msg.toByteArray()).joinToString("") { "%02x".format(it) }
    }

    /** POST /peer/hello to [host] for [pc], over [net] if given. [myIp] is our address as seen on that network. */
    fun hello(pc: Pc, host: String, myIp: String, net: Network?): Boolean {
        val id = Hub.deviceId
        val ts = System.currentTimeMillis().toString()
        val body = JSONObject().put("id", id).put("ip", myIp).put("port", Hub.HTTP_PORT).put("ts", ts.toLong())
            .put("sig", sign(pc.token, "$id|$myIp|$ts"))
        return try {
            val code = postJson(URL("http://$host:${pc.peerPort}/peer/hello"), body, net, 4000).first
            Log.i(TAG, "hello ${pc.name} @$host -> $code")
            code == 200
        } catch (e: Exception) {
            Log.i(TAG, "hello ${pc.name} @$host failed: $e")
            false
        }
    }

    /** A paired PC just talked to us. Over a LAN (not loopback / not the Wi-Fi Direct link) that proves the LAN path. */
    fun heardFrom(pc: Pc, remote: InetAddress?) {
        if (!pc.master || remote == null || remote.isLoopbackAddress) return
        if (remote.hostAddress == WfdClient.HOST) return
        masterFails.set(0)
        WfdClient.lanOk()
    }

    /** Small JSON POST helper; returns (status, body). */
    fun postJson(url: URL, body: JSONObject, net: Network?, timeoutMs: Int): Pair<Int, String> {
        val c = (if (net != null) net.openConnection(url) else url.openConnection()) as HttpURLConnection
        try {
            c.connectTimeout = timeoutMs
            c.readTimeout = timeoutMs
            c.requestMethod = "POST"
            c.doOutput = true
            c.setRequestProperty("Content-Type", "application/json")
            c.outputStream.use { it.write(body.toString().toByteArray()) }
            val code = c.responseCode
            val text = try { (if (code < 400) c.inputStream else c.errorStream)?.use { String(it.readBytes()) } ?: "" } catch (_: Exception) { "" }
            return code to text
        } finally { c.disconnect() }
    }
}

package dev.phonesync

import android.content.Context
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import android.net.wifi.WifiNetworkSpecifier
import android.util.Log
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/**
 * Wi-Fi Direct link. When the PC prefers it (Wfd.prefer) and this phone can stay on its own Wi-Fi at the same time
 * (STA + STA concurrency for local-only connections), the phone keeps the PC's direct network joined all the time:
 * a one-hop link that doesn't depend on the router. Otherwise it is a fallback: the PC hosts a Wi-Fi Direct "legacy" network (SSID + WPA2 passphrase, registered with
 * POST /api/pc/wfd or taken from the pairing QR). When the phone has been on no Wi-Fi at all for a while, the phone
 * requests that network with a WifiNetworkSpecifier (nothing is bound process-wide) and says hello to the PC at
 * 192.168.137.1:47801 over it. Released as soon as the LAN works again.
 *
 * Such a request opens Android's "choose a device" dialog over whatever is on screen, and cancelling it shows
 * "Something came up". So it is never made just because hellos failed while the phone is on Wi-Fi (that happens
 * after every unlock, before the LAN catches up), and is tried once per Wi-Fi outage.
 */
object WfdClient {
    private const val TAG = "SymphoneWfd"
    const val HOST = "192.168.137.1"
    private const val JOIN_TIMEOUT = 45_000L
    private const val BACKOFF = 10 * 60_000L
    /** How long the phone must be off Wi-Fi before it looks for the PC's direct network by itself. */
    private const val NO_WIFI_GRACE = 2 * 60_000L

    @Volatile var network: Network? = null
        private set
    @Volatile private var callback: ConnectivityManager.NetworkCallback? = null
    @Volatile private var current: Wfd? = null
    @Volatile private var pcId: String? = null
    @Volatile private var failedAt = 0L
    @Volatile private var joinLatch: CountDownLatch? = null
    /** A QR pairing is waiting on the network: automatic logic must not release it. */
    @Volatile private var joining = false
    @Volatile private var noWifiSince = 0L
    /** Already tried by itself during this Wi-Fi outage. */
    @Volatile private var autoTried = false

    private val cm by lazy { Hub.ctx.getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager }

    /** Joining the PC's network leaves the phone's own Wi-Fi (and internet) up. */
    val concurrent: Boolean by lazy {
        try {
            android.os.Build.VERSION.SDK_INT >= 31 && Hub.ctx.getSystemService(android.net.wifi.WifiManager::class.java)
                .isStaConcurrencyForLocalOnlyConnectionsSupported
        } catch (_: Exception) { false }
    }

    private fun preferred(w: Wfd?): Boolean = w != null && w.prefer && concurrent

    private val helloLoop = object : Runnable {
        override fun run() {
            val n = network ?: return
            val pc = Pcs.byId(pcId) ?: Pcs.master
            if (pc != null) Thread({ helloOver(n, pc) }, "wfd-hello").start()
            Arrival.handler.postDelayed(this, 60_000)
        }
    }

    /** Decide whether we should be on the master's Wi-Fi Direct network. Cheap; call often. */
    @Synchronized
    fun reevaluate() {
        if (joining) return
        if (callback != null) {
            // Holding a direct link: keep it while the PC it is for still has creds (LAN success calls lanOk()).
            val held = Pcs.byId(pcId)
            if (held == null || held.wfd == null) release("pc gone or no wfd")
            return
        }
        val now = System.currentTimeMillis()
        if (Arrival.wifiNetwork != null) { noWifiSince = 0; autoTried = false }
        else if (noWifiSince == 0L) noWifiSince = now
        val m = Pcs.master
        val w = m?.wfd ?: return
        val noLan = noWifiSince != 0L && now - noWifiSince >= NO_WIFI_GRACE && !autoTried
        if (preferred(w) && now - failedAt > BACKOFF) request(w, m.id)
        else if (noLan) { autoTried = true; request(w, m.id) }
    }

    /** The regular LAN path to the master works: drop the direct link (unless it is the preferred one). */
    fun lanOk() {
        if (callback != null && !joining && !preferred(Pcs.byId(pcId)?.wfd)) release("lan ok")
    }

    /**
     * Joins [w] (used by QR pairing) and waits until it is available. Returns the network or null.
     * Blocking: call off the main thread.
     */
    fun join(w: Wfd, forPc: String?, timeoutMs: Long = JOIN_TIMEOUT): Network? {
        joining = true
        synchronized(this) {
            if (callback != null && current == w) network?.let { joining = false; pcId = forPc; return it }
            if (callback != null) release("switch")
            failedAt = 0
            request(w, forPc)
        }
        try {
            joining = true
            joinLatch?.await(timeoutMs, TimeUnit.MILLISECONDS)
        } finally { joining = false }
        return network
    }

    @Synchronized
    private fun request(w: Wfd, forPc: String?) {
        val spec = try {
            WifiNetworkSpecifier.Builder().setSsid(w.ssid).setWpa2Passphrase(w.pass).build()
        } catch (e: Exception) { Log.w(TAG, "bad wfd creds: $e"); failedAt = System.currentTimeMillis(); return }
        val req = NetworkRequest.Builder()
            .addTransportType(NetworkCapabilities.TRANSPORT_WIFI)
            .removeCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)
            .setNetworkSpecifier(spec)
            .build()
        val latch = CountDownLatch(1)
        joinLatch = latch
        val cb = object : ConnectivityManager.NetworkCallback() {
            override fun onAvailable(n: Network) {
                Log.i(TAG, "joined ${w.ssid}: $n")
                network = n
                latch.countDown()
                Arrival.handler.removeCallbacks(helloLoop)
                Arrival.handler.post(helloLoop)
            }
            override fun onLost(n: Network) {
                Log.i(TAG, "lost ${w.ssid}")
                if (network == n) network = null
            }
            override fun onUnavailable() {
                Log.i(TAG, "unavailable ${w.ssid}")
                failedAt = System.currentTimeMillis()
                latch.countDown()
                synchronized(this@WfdClient) { if (callback === this) { callback = null; current = null } }
            }
        }
        try {
            cm.requestNetwork(req, cb)
            callback = cb
            current = w
            pcId = forPc
            Log.i(TAG, "requesting ${w.ssid}")
            // Give up if it never comes up (PC not hosting it / user declined).
            Arrival.handler.postDelayed({
                if (callback === cb && network == null) { failedAt = System.currentTimeMillis(); release("timeout") }
            }, JOIN_TIMEOUT)
        } catch (e: Exception) {
            Log.w(TAG, "request: $e")
            failedAt = System.currentTimeMillis()
            latch.countDown()
        }
    }

    @Synchronized
    fun release(why: String) {
        val cb = callback ?: return
        Log.i(TAG, "release ($why)")
        callback = null
        current = null
        network = null
        Arrival.handler.removeCallbacks(helloLoop)
        joinLatch?.countDown()
        try { cm.unregisterNetworkCallback(cb) } catch (_: Exception) {}
    }

    /** Our IPv4 on the direct network. */
    fun myIp(n: Network): String? = cm.getLinkProperties(n)?.let { Arrival.ipv4(it) }

    private fun helloOver(n: Network, pc: Pc) {
        val ip = myIp(n) ?: return
        Arrival.hello(pc, HOST, ip, n)
    }
}

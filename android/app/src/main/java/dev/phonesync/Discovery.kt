package dev.phonesync

import android.content.Context
import android.net.nsd.NsdManager
import android.net.nsd.NsdServiceInfo
import android.net.wifi.WifiManager
import android.util.Log
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.Inet4Address
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.NetworkInterface

/**
 * LAN discovery: answers "PHONESYNC?" UDP probes, broadcasts a beacon every few seconds,
 * and registers an mDNS/NSD service (_phonesync._tcp).
 */
object Discovery {
    private const val TAG = "SymphoneDisc"
    @Volatile private var running = false
    private var socket: DatagramSocket? = null
    private var mlock: WifiManager.MulticastLock? = null

    fun start() {
        if (running) return
        running = true
        val wm = Hub.ctx.applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager
        mlock = wm.createMulticastLock("phonesync").apply { setReferenceCounted(false); acquire() }
        val s = DatagramSocket(null).apply {
            reuseAddress = true
            broadcast = true
            bind(InetSocketAddress(Hub.UDP_PORT))
        }
        socket = s
        Thread({
            val buf = ByteArray(2048)
            while (running) {
                try {
                    val p = DatagramPacket(buf, buf.size)
                    s.receive(p)
                    val msg = String(p.data, 0, p.length)
                    if (msg.startsWith("PHONESYNC?")) {
                        val out = ("PHONESYNC!" + Hub.pingJson().put("ip", wifiIp() ?: "")).toByteArray()
                        s.send(DatagramPacket(out, out.size, p.address, p.port))
                    }
                } catch (e: Exception) {
                    if (running) Log.w(TAG, "recv: $e")
                }
            }
        }, "disc-recv").apply { isDaemon = true }.start()
        Thread({
            while (running) {
                try {
                    val ip = wifiIp()
                    if (ip != null) {
                        val out = ("PHONESYNC!" + Hub.pingJson().put("ip", ip)).toByteArray()
                        for (addr in broadcastAddrs()) s.send(DatagramPacket(out, out.size, addr, Hub.UDP_PORT))
                    }
                } catch (e: Exception) { Log.w(TAG, "beacon: $e") }
                Thread.sleep(4000)
            }
        }, "disc-beacon").apply { isDaemon = true }.start()
        registerNsd()
    }

    /** One immediate broadcast beacon (arrival burst). */
    fun beaconNow() {
        val s = socket ?: return
        val ip = Arrival.currentIp() ?: return
        Thread({
            try {
                val out = ("PHONESYNC!" + Hub.pingJson().put("ip", ip)).toByteArray()
                for (addr in broadcastAddrs()) s.send(DatagramPacket(out, out.size, addr, Hub.UDP_PORT))
            } catch (e: Exception) { Log.w(TAG, "beacon now: $e") }
        }, "disc-burst").start()
    }

    fun stop() {
        running = false
        socket?.close()
        mlock?.release()
    }

    private fun registerNsd() {
        try {
            val nsd = Hub.ctx.getSystemService(Context.NSD_SERVICE) as NsdManager
            val info = NsdServiceInfo().apply {
                serviceName = "Symphone-${Hub.deviceId}"
                serviceType = "_phonesync._tcp."
                port = Hub.HTTP_PORT
                setAttribute("name", Hub.deviceName)
                setAttribute("id", Hub.deviceId)
            }
            nsd.registerService(info, NsdManager.PROTOCOL_DNS_SD, object : NsdManager.RegistrationListener {
                override fun onServiceRegistered(i: NsdServiceInfo) {}
                override fun onRegistrationFailed(i: NsdServiceInfo, e: Int) { Log.w(TAG, "nsd failed $e") }
                override fun onServiceUnregistered(i: NsdServiceInfo) {}
                override fun onUnregistrationFailed(i: NsdServiceInfo, e: Int) {}
            })
        } catch (e: Exception) { Log.w(TAG, "nsd: $e") }
    }

    private fun wifiIface(): NetworkInterface? = try {
        NetworkInterface.getNetworkInterfaces().toList().firstOrNull { ni ->
            ni.isUp && !ni.isLoopback && (ni.name.startsWith("wlan") || ni.name.startsWith("ap") || ni.name.startsWith("swlan")) &&
                ni.inetAddresses.toList().any { it is Inet4Address }
        }
    } catch (_: Exception) { null }

    fun wifiIp(): String? = wifiIface()?.inetAddresses?.toList()?.firstOrNull { it is Inet4Address }?.hostAddress

    private fun broadcastAddrs(): List<InetAddress> {
        val out = ArrayList<InetAddress>()
        wifiIface()?.interfaceAddresses?.forEach { ia -> ia.broadcast?.let { out += it } }
        out += InetAddress.getByName("255.255.255.255")
        return out
    }
}

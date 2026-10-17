package dev.phonesync

import android.Manifest
import android.bluetooth.BluetoothManager
import android.bluetooth.BluetoothServerSocket
import android.bluetooth.BluetoothSocket
import android.content.pm.PackageManager
import android.util.Log
import java.util.UUID

/**
 * Bluetooth link to the PC for "keep the phone unlocked near this PC": the PC connects to this RFCOMM service and
 * keeps the connection open, so Android sees the PC as a connected Bluetooth device. With the PC added as a trusted
 * device in Android's Extend Unlock (Smart Lock), the phone then stays unlocked while it's near the PC.
 *
 * Nothing is exchanged but keep-alives; only devices bonded with the phone can connect (authenticated RFCOMM).
 */
object BtLink {
    private const val TAG = "SymphoneBt"
    val SERVICE: UUID = UUID.fromString("5f1c0d2e-8a4b-4c39-9e7a-53796d70686f")

    @Volatile private var thread: Thread? = null
    @Volatile private var server: BluetoothServerSocket? = null
    private val peers = java.util.concurrent.CopyOnWriteArrayList<BluetoothSocket>()

    /** Names of the PCs connected right now (for /api/info). */
    fun connected(): List<String> = peers.mapNotNull { s ->
        try { if (s.isConnected) (s.remoteDevice.name ?: s.remoteDevice.address) else null } catch (_: SecurityException) { null }
    }

    @Synchronized fun start() {
        if (thread?.isAlive == true) return
        if (Hub.ctx.checkSelfPermission(Manifest.permission.BLUETOOTH_CONNECT) != PackageManager.PERMISSION_GRANTED) return
        thread = Thread(::serve, "bt-link").apply { isDaemon = true; start() }
    }

    private fun serve() {
        while (true) {
            try {
                val adapter = Hub.ctx.getSystemService(BluetoothManager::class.java)?.adapter
                if (adapter == null || !adapter.isEnabled) { Thread.sleep(15_000); continue }
                val ss = adapter.listenUsingRfcommWithServiceRecord("Symphone", SERVICE)
                server = ss
                while (true) {
                    val s = ss.accept()
                    Log.i(TAG, "PC connected over Bluetooth")
                    peers.add(s)
                    Thread({ hold(s) }, "bt-peer").apply { isDaemon = true }.start()
                }
            } catch (e: SecurityException) {
                Log.w(TAG, "no Bluetooth permission: $e"); return
            } catch (e: Exception) {
                Log.i(TAG, "listen: $e")             // Bluetooth turned off, etc.: try again later
                try { server?.close() } catch (_: Exception) {}
                server = null
                Thread.sleep(10_000)
            }
        }
    }

    /** Answers the PC's keep-alives until it goes away. */
    private fun hold(s: BluetoothSocket) {
        try {
            val inp = s.inputStream; val out = s.outputStream
            val buf = ByteArray(64)
            while (true) {
                val n = inp.read(buf)
                if (n < 0) break
                out.write(buf, 0, n); out.flush()
            }
        } catch (_: Exception) {
        } finally {
            peers.remove(s)
            try { s.close() } catch (_: Exception) {}
            Log.i(TAG, "PC Bluetooth link closed")
        }
    }
}

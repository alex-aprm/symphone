package dev.phonesync

import android.util.Log
import java.io.BufferedInputStream
import java.io.BufferedOutputStream
import java.io.DataInputStream
import java.io.DataOutputStream
import java.net.InetSocketAddress
import java.net.ServerSocket
import java.net.Socket
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger

/**
 * Single-client framed TCP endpoint used by the sidecar activity while it is open.
 * The PC always connects to the phone (USB via `adb forward`, Wi-Fi via the phone's IP).
 *
 * Handshake: the PC sends one ASCII line `PSAUTH <token>\n` (same token as the HTTP API's X-Token).
 * The phone answers `OK\n` and from then on both sides exchange frames: `[u8 type][u32 BE length][payload]`.
 * A new authenticated client replaces the previous one.
 */
class SidecarLink(
    private val port: Int,
    private val tag: String,
    private val onFrame: (type: Int, payload: ByteArray, len: Int) -> Unit,
    private val onClient: (connected: Boolean) -> Unit,
) {
    @Volatile private var server: ServerSocket? = null
    @Volatile private var client: Socket? = null
    @Volatile private var running = false
    private val gen = AtomicInteger()
    private val outQ = LinkedBlockingQueue<ByteArray>(512)

    val connected get() = client != null

    fun start() {
        if (running) return
        running = true
        Thread({ acceptLoop() }, "$tag-accept").start()
    }

    fun stop() {
        running = false
        try { server?.close() } catch (_: Exception) {}
        try { client?.close() } catch (_: Exception) {}
        server = null
    }

    /** Queues a frame for the PC. Lossy when the link is congested (input frames are only useful when fresh). */
    fun send(type: Int, payload: ByteArray, droppable: Boolean = true) {
        if (client == null) return
        val f = ByteArray(5 + payload.size)
        f[0] = type.toByte()
        val n = payload.size
        f[1] = (n ushr 24).toByte(); f[2] = (n ushr 16).toByte(); f[3] = (n ushr 8).toByte(); f[4] = n.toByte()
        System.arraycopy(payload, 0, f, 5, n)
        if (!outQ.offer(f)) {
            if (!droppable) { outQ.poll(); outQ.offer(f) }
        }
    }

    fun sendJson(o: org.json.JSONObject) = send(3, o.toString().toByteArray(), droppable = false)

    private fun acceptLoop() {
        while (running) {
            try {
                val ss = ServerSocket()
                ss.reuseAddress = true
                ss.bind(InetSocketAddress(port))
                server = ss
                Log.i("Symphone", "$tag listening on $port")
                while (running) {
                    val s = ss.accept()
                    Thread({ handle(s) }, "$tag-client").start()
                }
            } catch (e: Exception) {
                if (running) {
                    Log.w("Symphone", "$tag server: $e")
                    try { Thread.sleep(500) } catch (_: InterruptedException) {}
                }
            }
        }
    }

    private fun handle(s: Socket) {
        try {
            s.tcpNoDelay = true
            s.soTimeout = 5000
            s.receiveBufferSize = 4 shl 20
            s.sendBufferSize = 256 shl 10
            val inp = DataInputStream(BufferedInputStream(s.getInputStream(), 1 shl 20))
            val line = StringBuilder()
            while (line.length < 256) {
                val c = inp.read()
                if (c < 0) throw java.io.EOFException()
                if (c == '\n'.code) break
                line.append(c.toChar())
            }
            val parts = line.toString().trim().split(" ")
            if (parts.size < 2 || parts[0] != "PSAUTH" || Pcs.byToken(parts[1]) == null) {
                s.getOutputStream().write("ERR auth\n".toByteArray()); s.close(); return
            }
            val out = DataOutputStream(BufferedOutputStream(s.getOutputStream(), 64 shl 10))
            out.write("OK\n".toByteArray()); out.flush()
            s.soTimeout = 0
            // Replace the previous client.
            val old = client
            client = s
            val my = gen.incrementAndGet()
            try { old?.close() } catch (_: Exception) {}
            outQ.clear()
            onClient(true)
            val writer = Thread({
                try {
                    while (gen.get() == my && !s.isClosed) {
                        val f = outQ.poll(250, TimeUnit.MILLISECONDS) ?: continue
                        out.write(f)
                        // Drain whatever else is queued, then flush once.
                        while (true) { val g = outQ.poll() ?: break; out.write(g) }
                        out.flush()
                    }
                } catch (_: Exception) {
                } finally { try { s.close() } catch (_: Exception) {} }
            }, "$tag-writer")
            writer.start()
            var buf = ByteArray(64 shl 10)
            while (true) {
                val type = inp.read()
                if (type < 0) break
                val len = inp.readInt()
                if (len < 0 || len > (16 shl 20)) break
                if (len > buf.size) buf = ByteArray(len + (len shr 2))
                inp.readFully(buf, 0, len)
                try { onFrame(type, buf, len) } catch (e: Exception) { Log.w("Symphone", "$tag frame: $e") }
            }
        } catch (_: Exception) {
        } finally {
            try { s.close() } catch (_: Exception) {}
            if (client === s) {
                client = null
                onClient(false)
            }
        }
    }
}

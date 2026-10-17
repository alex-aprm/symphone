package dev.phonesync

import android.util.Log
import java.io.ByteArrayOutputStream
import java.io.EOFException
import java.io.IOException
import java.io.InputStream
import java.net.InetSocketAddress
import java.net.StandardSocketOptions
import java.nio.ByteBuffer
import java.nio.channels.FileChannel
import java.nio.channels.ServerSocketChannel
import java.nio.channels.SocketChannel
import java.security.MessageDigest
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.TimeZone
import java.util.concurrent.Executors
import java.util.concurrent.ThreadFactory
import java.util.concurrent.atomic.AtomicInteger
import kotlin.math.min

/**
 * Small blocking-NIO HTTP/1.1 server tuned for bulk transfer:
 * keep-alive, zero-copy sendfile for downloads (FileChannel.transferTo into the SocketChannel)
 * and direct-buffer writes for uploads.
 */
class HttpServer(private val port: Int) {
    private val tag = "SymphoneHttp"
    @Volatile private var server: ServerSocketChannel? = null
    private val threadNo = AtomicInteger()
    private val pool = Executors.newCachedThreadPool(ThreadFactory { r ->
        Thread(r, "http-${threadNo.incrementAndGet()}").apply { isDaemon = true }
    })

    fun start() {
        if (server != null) return
        val ss = ServerSocketChannel.open()
        ss.setOption(StandardSocketOptions.SO_REUSEADDR, true)
        ss.bind(InetSocketAddress(port), 256)
        server = ss
        Thread({
            while (true) {
                val sc = try { ss.accept() } catch (e: IOException) { break }
                pool.execute { serve(sc) }
            }
        }, "http-accept").apply { isDaemon = true }.start()
        Log.i(tag, "listening on $port")
    }

    fun stop() {
        try { server?.close() } catch (_: IOException) {}
        server = null
    }

    private fun serve(sc: SocketChannel) {
        sc.socket().tcpNoDelay = true
        val conn = Conn(sc)
        try {
            while (true) {
                val req = Request.read(conn) ?: break
                val resp = Response(conn, req)
                try {
                    Router.handle(req, resp)
                } catch (e: Throwable) {
                    if (e is IOException && resp.started) throw e
                    Log.w(tag, "${req.method} ${req.path}: $e", e)
                    if (!resp.started) resp.text(500, "Internal error: $e")
                    else break
                }
                if (!resp.started) resp.status(204)
                if (resp.closeAfter || req.wantsClose) break
                if (!req.drainBody()) break
            }
        } catch (_: IOException) {
        } catch (_: EOFException) {
        } finally {
            try { sc.close() } catch (_: IOException) {}
        }
    }
}

class Conn(val sc: SocketChannel) {
    private val buf: ByteBuffer = ByteBuffer.allocateDirect(64 * 1024).also { it.flip() }

    private fun fill(): Boolean {
        buf.compact()
        val n = try { sc.read(buf) } finally { buf.flip() }
        return n > 0
    }

    fun readLine(): String? {
        val sb = StringBuilder()
        while (true) {
            if (!buf.hasRemaining() && !fill()) return if (sb.isEmpty()) null else sb.toString()
            val c = buf.get().toInt() and 0xff
            if (c == '\n'.code) {
                if (sb.isNotEmpty() && sb[sb.length - 1] == '\r') sb.setLength(sb.length - 1)
                return sb.toString()
            }
            sb.append(c.toChar())
            if (sb.length > 32768) throw IOException("header line too long")
        }
    }

    /** Reads up to dst.remaining() bytes; buffered bytes first, then straight from the socket. */
    fun read(dst: ByteBuffer): Int {
        if (buf.hasRemaining()) {
            val n = min(buf.remaining(), dst.remaining())
            val lim = buf.limit()
            buf.limit(buf.position() + n)
            dst.put(buf)
            buf.limit(lim)
            return n
        }
        return sc.read(dst)
    }

    fun write(bb: ByteBuffer) { while (bb.hasRemaining()) sc.write(bb) }
    fun write(bytes: ByteArray) = write(ByteBuffer.wrap(bytes))
}

class Request(
    val conn: Conn,
    val method: String,
    val rawPath: String,
    val path: String,
    val query: Map<String, String>,
    val headers: Map<String, String>,
) {
    private var remaining: Long = headers["content-length"]?.trim()?.toLongOrNull() ?: 0L
    private val chunked = headers["transfer-encoding"]?.contains("chunked", true) == true
    private var chunkLeft = 0L
    private var chunkDone = false

    val contentLength: Long? = if (chunked) null else remaining
    val wantsClose = headers["connection"]?.equals("close", true) == true
    val hasBody get() = chunked || (contentLength ?: 0) > 0

    fun header(name: String) = headers[name.lowercase()]

    /** The paired PC this request is authenticated as (set by the Router). */
    var pc: Pc? = null

    val remote: java.net.InetAddress?
        get() = try { (conn.sc.remoteAddress as? InetSocketAddress)?.address } catch (_: Exception) { null }

    /** Body reader for both Content-Length and chunked transfer encodings. Returns -1 at end. */
    fun readBody(dst: ByteBuffer): Int {
        if (!chunked) {
            if (remaining <= 0) return -1
            val lim = dst.limit()
            if (dst.remaining() > remaining) dst.limit(dst.position() + remaining.toInt())
            val n = conn.read(dst)
            dst.limit(lim)
            if (n < 0) throw EOFException()
            remaining -= n
            return n
        }
        if (chunkDone) return -1
        if (chunkLeft == 0L) {
            val line = conn.readLine() ?: throw EOFException()
            chunkLeft = line.substringBefore(';').trim().toLong(16)
            if (chunkLeft == 0L) {
                while (true) { val t = conn.readLine() ?: break; if (t.isEmpty()) break }
                chunkDone = true
                return -1
            }
        }
        val lim = dst.limit()
        if (dst.remaining() > chunkLeft) dst.limit(dst.position() + chunkLeft.toInt())
        val n = conn.read(dst)
        dst.limit(lim)
        if (n < 0) throw EOFException()
        chunkLeft -= n
        if (chunkLeft == 0L) conn.readLine()
        return n
    }

    fun bodyBytes(max: Int = 16 shl 20): ByteArray {
        val out = ByteArrayOutputStream()
        val bb = ByteBuffer.allocate(64 * 1024)
        while (true) {
            bb.clear()
            val n = readBody(bb)
            if (n < 0) break
            out.write(bb.array(), 0, n)
            if (out.size() > max) throw IOException("body too large")
        }
        return out.toByteArray()
    }

    fun bodyText() = String(bodyBytes(), Charsets.UTF_8)

    /** Streams the body into a file channel. Returns bytes written. */
    fun bodyTo(fc: FileChannel): Long {
        val bb = ByteBuffer.allocateDirect(1 shl 20)
        var total = 0L
        while (true) {
            bb.clear()
            val n = readBody(bb)
            if (n < 0) break
            bb.flip()
            while (bb.hasRemaining()) total += fc.write(bb)
        }
        return total
    }

    fun bodyStream(): InputStream = object : InputStream() {
        private val one = ByteBuffer.allocate(1)
        override fun read(): Int {
            one.clear()
            return if (readBody(one) < 0) -1 else one.get(0).toInt() and 0xff
        }
        override fun read(b: ByteArray, off: Int, len: Int): Int {
            if (len == 0) return 0
            return readBody(ByteBuffer.wrap(b, off, len))
        }
    }

    /** Consumes unread body so the connection can be reused; false if it should be closed. */
    fun drainBody(): Boolean {
        if (!chunked && remaining > 8L * 1024 * 1024) return false
        return try {
            val bb = ByteBuffer.allocate(64 * 1024)
            while (true) { bb.clear(); if (readBody(bb) < 0) break }
            true
        } catch (_: IOException) { false }
    }

    companion object {
        fun read(conn: Conn): Request? {
            var line = conn.readLine() ?: return null
            while (line.isEmpty()) line = conn.readLine() ?: return null
            val parts = line.split(' ')
            if (parts.size < 2) throw IOException("bad request line")
            val headers = HashMap<String, String>()
            while (true) {
                val h = conn.readLine() ?: throw EOFException()
                if (h.isEmpty()) break
                val i = h.indexOf(':')
                if (i > 0) headers[h.substring(0, i).trim().lowercase()] = h.substring(i + 1).trim()
            }
            val target = parts[1]
            val q = target.indexOf('?')
            val rawPath = if (q >= 0) target.substring(0, q) else target
            val query = HashMap<String, String>()
            if (q >= 0) {
                for (kv in target.substring(q + 1).split('&')) {
                    if (kv.isEmpty()) continue
                    val e = kv.indexOf('=')
                    if (e < 0) query[Http.decode(kv)] = ""
                    else query[Http.decode(kv.substring(0, e))] = Http.decode(kv.substring(e + 1).replace('+', ' '))
                }
            }
            return Request(conn, parts[0].uppercase(), rawPath, Http.decode(rawPath), query, headers)
        }
    }
}

class Response(private val conn: Conn, private val req: Request) {
    var started = false
        private set
    var closeAfter = false

    fun begin(status: Int, headers: List<Pair<String, String>>, length: Long?) {
        check(!started)
        started = true
        val sb = StringBuilder(256)
        sb.append("HTTP/1.1 ").append(status).append(' ').append(Http.reason(status)).append("\r\n")
        for ((k, v) in headers) sb.append(k).append(": ").append(v).append("\r\n")
        if (length != null) sb.append("Content-Length: ").append(length).append("\r\n")
        else { sb.append("Connection: close\r\n"); closeAfter = true }
        sb.append("Server: Symphone\r\n\r\n")
        conn.write(sb.toString().toByteArray(Charsets.UTF_8))
    }

    fun bytes(status: Int, body: ByteArray, type: String, headers: List<Pair<String, String>> = emptyList()) {
        begin(status, headers + ("Content-Type" to type), body.size.toLong())
        if (req.method != "HEAD" && body.isNotEmpty()) conn.write(body)
    }

    fun text(status: Int, s: String) = bytes(status, s.toByteArray(), "text/plain; charset=utf-8")
    fun json(any: Any, status: Int = 200) = bytes(status, any.toString().toByteArray(), "application/json; charset=utf-8")
    fun status(status: Int, headers: List<Pair<String, String>> = emptyList()) = begin(status, headers, 0)

    /** Writes raw bytes after begin(); used by streaming responses (SSE). */
    fun write(b: ByteArray) = conn.write(b)

    /** Zero-copy file (range) transfer. */
    fun file(fc: FileChannel, size: Long, type: String, mtime: Long, extra: List<Pair<String, String>> = emptyList()) {
        var start = 0L
        var end = size - 1
        var status = 200
        val headers = ArrayList<Pair<String, String>>(extra)
        headers += "Accept-Ranges" to "bytes"
        headers += "Content-Type" to type
        headers += "Last-Modified" to Http.httpDate(mtime)
        headers += "ETag" to Http.etag(size, mtime)
        val range = req.header("range")
        if (range != null && range.startsWith("bytes=") && !range.contains(',')) {
            val spec = range.substring(6).trim()
            val dash = spec.indexOf('-')
            val a = spec.substring(0, dash).trim()
            val b = spec.substring(dash + 1).trim()
            if (a.isEmpty()) { val n = b.toLong(); start = maxOf(0, size - n) }
            else { start = a.toLong(); if (b.isNotEmpty()) end = minOf(b.toLong(), size - 1) }
            if (start >= size || start > end) {
                begin(416, listOf("Content-Range" to "bytes */$size"), 0)
                return
            }
            status = 206
            headers += "Content-Range" to "bytes $start-$end/$size"
        }
        val len = if (size == 0L) 0 else end - start + 1
        begin(status, headers, len)
        if (req.method == "HEAD") return
        var pos = start
        val limit = start + len
        while (pos < limit) {
            val n = fc.transferTo(pos, minOf(limit - pos, 8L shl 20), conn.sc)
            if (n <= 0) {
                // Some file systems (FUSE) refuse sendfile: fall back to buffered copy.
                val bb = ByteBuffer.allocateDirect(1 shl 20)
                while (pos < limit) {
                    bb.clear()
                    if (limit - pos < bb.capacity()) bb.limit((limit - pos).toInt())
                    val r = fc.read(bb, pos)
                    if (r <= 0) throw EOFException()
                    bb.flip(); conn.write(bb); pos += r
                }
                break
            }
            pos += n
        }
    }
}

object Http {
    private val hex = "0123456789ABCDEF"

    fun decode(s: String): String {
        if (s.indexOf('%') < 0) return String(s.toByteArray(Charsets.ISO_8859_1), Charsets.UTF_8)
        val src = s.toByteArray(Charsets.ISO_8859_1)
        val out = ByteArrayOutputStream(src.size)
        var i = 0
        while (i < src.size) {
            val c = src[i].toInt()
            if (c == '%'.code && i + 2 < src.size) {
                val h = Character.digit(src[i + 1].toInt(), 16)
                val l = Character.digit(src[i + 2].toInt(), 16)
                if (h >= 0 && l >= 0) { out.write(h * 16 + l); i += 3; continue }
            }
            out.write(c); i++
        }
        return String(out.toByteArray(), Charsets.UTF_8)
    }

    /** Percent-encodes a path, keeping '/' and unreserved characters. */
    fun encodePath(p: String): String {
        val sb = StringBuilder(p.length + 16)
        for (b in p.toByteArray(Charsets.UTF_8)) {
            val c = b.toInt() and 0xff
            if ((c in 'a'.code..'z'.code) || (c in 'A'.code..'Z'.code) || (c in '0'.code..'9'.code) ||
                c == '/'.code || c == '-'.code || c == '_'.code || c == '.'.code || c == '~'.code
            ) sb.append(c.toChar())
            else sb.append('%').append(hex[c shr 4]).append(hex[c and 15])
        }
        return sb.toString()
    }

    private val httpFmt = ThreadLocal.withInitial {
        SimpleDateFormat("EEE, dd MMM yyyy HH:mm:ss 'GMT'", Locale.US).apply { timeZone = TimeZone.getTimeZone("GMT") }
    }
    private val isoFmt = ThreadLocal.withInitial {
        SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss'Z'", Locale.US).apply { timeZone = TimeZone.getTimeZone("GMT") }
    }

    fun httpDate(ms: Long): String = httpFmt.get()!!.format(Date(ms))
    fun isoDate(ms: Long): String = isoFmt.get()!!.format(Date(ms))
    fun etag(size: Long, mtime: Long) = "\"${java.lang.Long.toHexString(mtime)}-${java.lang.Long.toHexString(size)}\""

    fun constEq(a: String, b: String) =
        MessageDigest.isEqual(a.toByteArray(), b.toByteArray())

    fun reason(code: Int) = when (code) {
        200 -> "OK"; 201 -> "Created"; 204 -> "No Content"; 206 -> "Partial Content"; 207 -> "Multi-Status"
        301 -> "Moved Permanently"; 304 -> "Not Modified"
        400 -> "Bad Request"; 401 -> "Unauthorized"; 403 -> "Forbidden"; 404 -> "Not Found"
        405 -> "Method Not Allowed"; 409 -> "Conflict"; 412 -> "Precondition Failed"; 415 -> "Unsupported Media Type"
        416 -> "Range Not Satisfiable"; 423 -> "Locked"; 429 -> "Too Many Requests"
        500 -> "Internal Server Error"; 501 -> "Not Implemented"; 503 -> "Service Unavailable"; 507 -> "Insufficient Storage"
        else -> "Status"
    }

    fun mime(name: String): String {
        val ext = name.substringAfterLast('.', "").lowercase()
        return android.webkit.MimeTypeMap.getSingleton().getMimeTypeFromExtension(ext) ?: when (ext) {
            "heic" -> "image/heic"; "heif" -> "image/heif"; "webp" -> "image/webp"; "mkv" -> "video/x-matroska"
            else -> "application/octet-stream"
        }
    }
}

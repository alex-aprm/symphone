package dev.phonesync

import android.media.MediaScannerConnection
import android.os.Environment
import android.os.StatFs
import java.io.File
import java.io.IOException
import java.nio.channels.FileChannel
import java.nio.file.Files
import java.nio.file.LinkOption
import java.nio.file.StandardOpenOption
import java.nio.file.attribute.BasicFileAttributes
import java.nio.file.attribute.FileTime
import java.text.SimpleDateFormat
import java.util.Locale
import java.util.TimeZone
import java.util.UUID

/** WebDAV class 1/2 (fake locks) over shared storage. Enough for rclone, Windows WebClient and file managers. */
object WebDav {
    val root: File = Environment.getExternalStorageDirectory()
    private val mediaExt = setOf("jpg", "jpeg", "png", "gif", "webp", "heic", "heif", "dng", "mp4", "mkv", "mov", "3gp", "webm", "mp3", "m4a", "flac", "ogg", "wav", "opus", "aac")

    fun resolve(sub: String): File? {
        val clean = sub.trim('/')
        if (clean.isEmpty()) return root
        if (clean.split('/').any { it == ".." || it == "." }) return null
        return File(root, clean)
    }

    fun handle(req: Request, resp: Response, sub: String, prefix: String) {
        val f = resolve(sub) ?: return resp.text(400, "bad path")
        when (req.method) {
            "OPTIONS" -> resp.status(200, listOf(
                "DAV" to "1, 2", "MS-Author-Via" to "DAV",
                "Allow" to "OPTIONS, GET, HEAD, PUT, DELETE, PROPFIND, PROPPATCH, MKCOL, COPY, MOVE, LOCK, UNLOCK"))
            "PROPFIND" -> propfind(req, resp, f, sub, prefix)
            "GET", "HEAD" -> get(req, resp, f, sub, prefix)
            "PUT" -> put(req, resp, f)
            "DELETE" -> delete(resp, f)
            "MKCOL" -> mkcol(resp, f)
            "MOVE", "COPY" -> moveCopy(req, resp, f, req.method == "MOVE")
            "LOCK" -> lock(req, resp, sub, prefix)
            "UNLOCK" -> resp.status(204)
            "PROPPATCH" -> proppatch(req, resp, f, sub, prefix)
            else -> resp.status(405)
        }
    }

    private fun attrs(f: File): BasicFileAttributes? = try {
        Files.readAttributes(f.toPath(), BasicFileAttributes::class.java, LinkOption.NOFOLLOW_LINKS)
    } catch (_: IOException) { null }

    private fun propfind(req: Request, resp: Response, f: File, sub: String, prefix: String) {
        req.drainBody()
        val a = attrs(f) ?: return resp.text(404, "not found")
        val depth = req.header("depth") ?: "1"
        val sb = StringBuilder(8192)
        sb.append("<?xml version=\"1.0\" encoding=\"utf-8\"?>\n<D:multistatus xmlns:D=\"DAV:\" xmlns:Z=\"urn:schemas-microsoft-com:\">")
        val base = "/" + sub.trim('/')
        entry(sb, prefix, base, f, a, isRoot = base == "/")
        if (a.isDirectory && depth != "0") {
            try {
                Files.newDirectoryStream(f.toPath()).use { ds ->
                    for (p in ds) {
                        val ca = try { Files.readAttributes(p, BasicFileAttributes::class.java) } catch (_: IOException) { continue }
                        val name = p.fileName.toString()
                        entry(sb, prefix, if (base == "/") "/$name" else "$base/$name", p.toFile(), ca, false)
                    }
                }
            } catch (_: IOException) {}
        }
        sb.append("</D:multistatus>")
        resp.bytes(207, sb.toString().toByteArray(), "application/xml; charset=utf-8")
    }

    private fun entry(sb: StringBuilder, prefix: String, rel: String, f: File, a: BasicFileAttributes, isRoot: Boolean) {
        val dir = a.isDirectory
        var href = Http.encodePath(prefix + rel)
        if (dir && !href.endsWith("/")) href += "/"
        val mtime = a.lastModifiedTime().toMillis()
        val name = if (isRoot) "Phone" else f.name
        sb.append("<D:response><D:href>").append(href).append("</D:href><D:propstat><D:prop>")
        sb.append("<D:displayname>").append(xml(name)).append("</D:displayname>")
        sb.append("<D:getlastmodified>").append(Http.httpDate(mtime)).append("</D:getlastmodified>")
        sb.append("<D:creationdate>").append(Http.isoDate(mtime)).append("</D:creationdate>")
        if (dir) {
            sb.append("<D:resourcetype><D:collection/></D:resourcetype>")
            if (isRoot) {
                val st = StatFs(root.path)
                sb.append("<D:quota-available-bytes>").append(st.availableBytes).append("</D:quota-available-bytes>")
                sb.append("<D:quota-used-bytes>").append(st.totalBytes - st.availableBytes).append("</D:quota-used-bytes>")
            }
        } else {
            sb.append("<D:resourcetype/>")
            sb.append("<D:getcontentlength>").append(a.size()).append("</D:getcontentlength>")
            sb.append("<D:getcontenttype>").append(Http.mime(f.name)).append("</D:getcontenttype>")
            sb.append("<D:getetag>").append(Http.etag(a.size(), mtime)).append("</D:getetag>")
        }
        if (f.name.startsWith(".") && !isRoot) sb.append("<Z:Win32FileAttributes>00000002</Z:Win32FileAttributes>")
        sb.append("<D:supportedlock><D:lockentry><D:lockscope><D:exclusive/></D:lockscope><D:locktype><D:write/></D:locktype></D:lockentry></D:supportedlock>")
        sb.append("</D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>")
    }

    private fun get(req: Request, resp: Response, f: File, sub: String, prefix: String) {
        val a = attrs(f) ?: return resp.text(404, "not found")
        if (a.isDirectory) {
            val sb = StringBuilder("<!doctype html><meta charset=utf-8><title>${xml(f.name)}</title><h3>${xml(sub)}</h3><ul>")
            f.listFiles()?.sortedBy { it.name.lowercase() }?.forEach {
                val n = it.name + if (it.isDirectory) "/" else ""
                sb.append("<li><a href=\"").append(Http.encodePath("$prefix/${sub.trim('/')}/".replace("//", "/") + it.name))
                    .append(if (it.isDirectory) "/" else "").append("\">").append(xml(n)).append("</a>")
            }
            return resp.bytes(200, sb.toString().toByteArray(), "text/html; charset=utf-8")
        }
        val etag = Http.etag(a.size(), a.lastModifiedTime().toMillis())
        if (req.header("if-none-match") == etag) return resp.status(304)
        FileChannel.open(f.toPath(), StandardOpenOption.READ).use { fc ->
            resp.file(fc, a.size(), Http.mime(f.name), a.lastModifiedTime().toMillis())
        }
    }

    private fun put(req: Request, resp: Response, f: File) {
        if (f.isDirectory) return resp.status(405)
        val parent = f.parentFile ?: return resp.status(409)
        if (!parent.isDirectory) return resp.status(409)
        val existed = f.exists()
        try {
            FileChannel.open(f.toPath(), StandardOpenOption.CREATE, StandardOpenOption.WRITE, StandardOpenOption.TRUNCATE_EXISTING).use { fc ->
                req.bodyTo(fc)
            }
        } catch (e: IOException) {
            return resp.text(if (e.message?.contains("ENOSPC") == true) 507 else 500, e.toString())
        }
        val headers = ArrayList<Pair<String, String>>()
        req.header("x-oc-mtime")?.toLongOrNull()?.let {
            f.setLastModified(it * 1000)
            headers += "X-OC-Mtime" to "accepted"
        }
        scan(f)
        resp.status(if (existed) 204 else 201, headers)
    }

    private fun delete(resp: Response, f: File) {
        if (!f.exists()) return resp.status(404)
        if (f == root) return resp.status(403)
        val ok = f.deleteRecursively()
        scan(f)
        resp.status(if (ok) 204 else 500)
    }

    private fun mkcol(resp: Response, f: File) {
        if (f.exists()) return resp.status(405)
        if (f.parentFile?.isDirectory != true) return resp.status(409)
        resp.status(if (f.mkdir()) 201 else 500)
    }

    private fun moveCopy(req: Request, resp: Response, src: File, move: Boolean) {
        if (!src.exists()) return resp.status(404)
        val destHeader = req.header("destination") ?: return resp.status(400)
        var p = destHeader
        val scheme = p.indexOf("://")
        if (scheme >= 0) p = p.substring(p.indexOf('/', scheme + 3).let { if (it < 0) p.length else it })
        p = Http.decode(p)
        if (p.startsWith("/t/")) p = p.substring(p.indexOf('/', 3).let { if (it < 0) p.length else it })
        if (!p.startsWith("/dav")) return resp.status(400)
        val dst = resolve(p.removePrefix("/dav")) ?: return resp.status(400)
        if (dst == src) return resp.status(403)
        val overwrite = req.header("overwrite")?.uppercase() != "F"
        val existed = dst.exists()
        if (existed && !overwrite) return resp.status(412)
        if (dst.parentFile?.isDirectory != true) return resp.status(409)
        if (existed) dst.deleteRecursively()
        val ok = if (move) {
            src.renameTo(dst) || (src.copyRecursively(dst, true) && src.deleteRecursively())
        } else src.copyRecursively(dst, true)
        if (move) scan(src)
        scan(dst)
        resp.status(if (!ok) 500 else if (existed) 204 else 201)
    }

    private fun lock(req: Request, resp: Response, sub: String, prefix: String) {
        val body = req.bodyText()
        val owner = Regex("<(?:\\w+:)?owner>(.*?)</(?:\\w+:)?owner>", RegexOption.DOT_MATCHES_ALL).find(body)?.groupValues?.get(1) ?: ""
        val token = "opaquelocktoken:" + UUID.randomUUID()
        val x = "<?xml version=\"1.0\" encoding=\"utf-8\"?><D:prop xmlns:D=\"DAV:\"><D:lockdiscovery><D:activelock>" +
            "<D:locktype><D:write/></D:locktype><D:lockscope><D:exclusive/></D:lockscope><D:depth>0</D:depth>" +
            "<D:owner>$owner</D:owner><D:timeout>Second-3600</D:timeout>" +
            "<D:locktoken><D:href>$token</D:href></D:locktoken>" +
            "<D:lockroot><D:href>${Http.encodePath(prefix + "/" + sub.trim('/'))}</D:href></D:lockroot>" +
            "</D:activelock></D:lockdiscovery></D:prop>"
        resp.bytes(200, x.toByteArray(), "application/xml; charset=utf-8", listOf("Lock-Token" to "<$token>"))
    }

    private val win32Fmt = ThreadLocal.withInitial {
        SimpleDateFormat("EEE, dd MMM yyyy HH:mm:ss zzz", Locale.US).apply { timeZone = TimeZone.getTimeZone("GMT") }
    }

    private fun proppatch(req: Request, resp: Response, f: File, sub: String, prefix: String) {
        val body = req.bodyText()
        // Windows sends Win32LastModifiedTime when copying; honour it so dates survive the copy.
        Regex("Win32LastModifiedTime[^>]*>([^<]+)<").find(body)?.groupValues?.get(1)?.let {
            try { win32Fmt.get()!!.parse(it.trim())?.let { d -> Files.setLastModifiedTime(f.toPath(), FileTime.fromMillis(d.time)) } } catch (_: Exception) {}
        }
        val names = Regex("<(\\w+):(Win32\\w+|getlastmodified)[ />]").findAll(body).map { it.groupValues[2] }.toSet()
        val props = names.joinToString("") { if (it == "getlastmodified") "<D:$it/>" else "<Z:$it/>" }
        val x = "<?xml version=\"1.0\" encoding=\"utf-8\"?><D:multistatus xmlns:D=\"DAV:\" xmlns:Z=\"urn:schemas-microsoft-com:\">" +
            "<D:response><D:href>${Http.encodePath(prefix + "/" + sub.trim('/'))}</D:href><D:propstat><D:prop>$props</D:prop>" +
            "<D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response></D:multistatus>"
        resp.bytes(207, x.toByteArray(), "application/xml; charset=utf-8")
    }

    fun scan(f: File) {
        val ext = f.name.substringAfterLast('.', "").lowercase()
        if (f.isDirectory || ext in mediaExt || !f.exists()) {
            MediaScannerConnection.scanFile(Hub.ctx, arrayOf(f.absolutePath), null, null)
        }
    }

    fun xml(s: String) = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\"", "&quot;")
}

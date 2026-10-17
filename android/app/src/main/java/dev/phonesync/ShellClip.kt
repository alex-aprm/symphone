package dev.phonesync

import android.content.ClipData
import android.net.LocalServerSocket
import android.os.IBinder
import android.os.IInterface
import java.io.File
import java.lang.reflect.Method
import java.net.HttpURLConnection
import java.net.URL

/**
 * Shell clipboard helper. Android 10+ blocks background clipboard reads for apps, but the shell user may read it.
 * Started by the PC over adb (runs as uid shell, outside the app process):
 *
 *   CLASSPATH=$(pm path dev.phonesync | cut -d: -f2) nohup app_process / dev.phonesync.ShellClip <token> >/dev/null 2>&1 &
 *
 * Polls the clipboard as package "com.android.shell" every 500 ms through the hidden IClipboard binder and POSTs
 * changes to http://127.0.0.1:47900/api/clip/shell with X-Token. Single instance (abstract socket lock); exits when
 * the token is rejected or the APK it was loaded from is replaced/uninstalled.
 *
 * Must not touch Hub or anything that needs an app Context.
 */
object ShellClip {
    private const val PKG = "com.android.shell"
    const val PC_LABEL = "PC"                 // label of clips set by the app from the PC (Api.kt)
    private const val URL_CLIP = "http://127.0.0.1:47900/api/clip/shell"

    @JvmStatic
    fun main(args: Array<String>) {
        val token = args.firstOrNull()?.trim().orEmpty()
        if (token.isEmpty()) { System.err.println("usage: ShellClip <token>"); System.exit(2) }
        val lock = try { LocalServerSocket("phonesync-shellclip") } catch (_: Exception) {
            println("ShellClip: already running"); System.exit(0); return
        }
        val apk = System.getenv("CLASSPATH")?.split(':')?.firstOrNull { it.endsWith(".apk") }?.let { File(it) }
        val reader = try { Reader() } catch (e: Throwable) {
            System.err.println("ShellClip: no clipboard access: $e"); System.exit(1); return
        }
        println("ShellClip: running (${reader.describe()})")
        // Reading a clip (getPrimaryClip) makes Android toast "Shell pasted from your clipboard", so only the description
        // (no toast) is polled; the text is read once per new clip, and never for clips the PC itself put there.
        var stamp = try { reader.stamp() } catch (_: Throwable) { null }  // don't send what was there before we started
        var last: String? = null
        var rejected = 0
        while (true) {
            try { Thread.sleep(500) } catch (_: InterruptedException) { break }
            if (apk != null && !apk.exists()) { println("ShellClip: apk gone, exiting"); break }
            val st = try { reader.stamp() } catch (e: Throwable) { System.err.println("ShellClip: desc: $e"); null }
            if (st == null || st == stamp) continue
            stamp = st
            if (st.second == PC_LABEL) continue
            val t = try { reader.text() } catch (e: Throwable) { System.err.println("ShellClip: read: $e"); null }
            if (t.isNullOrEmpty() || t == last) continue
            when (val code = post(token, t)) {
                200, 204 -> { last = t; rejected = 0 }
                401, 403 -> { stamp = null; if (++rejected >= 3) { println("ShellClip: token rejected, exiting"); break } }
                else -> { /* service not reachable right now: retry on the next tick */ stamp = null; if (code == -1) Thread.sleep(2000) }
            }
        }
        try { lock.close() } catch (_: Exception) {}
        System.exit(0)
    }

    private fun post(token: String, text: String): Int = try {
        val c = URL(URL_CLIP).openConnection() as HttpURLConnection
        try {
            c.connectTimeout = 2000; c.readTimeout = 4000
            c.requestMethod = "POST"; c.doOutput = true
            c.setRequestProperty("X-Token", token)
            c.setRequestProperty("Content-Type", "application/json; charset=utf-8")
            val body = org.json.JSONObject().put("text", text).toString().toByteArray(Charsets.UTF_8)
            c.setFixedLengthStreamingMode(body.size)
            c.outputStream.use { it.write(body) }
            c.responseCode.also { try { c.inputStream.use { s -> s.readBytes() } } catch (_: Exception) {} }
        } finally { c.disconnect() }
    } catch (_: Exception) { -1 }

    /** Reflection on IClipboard.getPrimaryClip; the signature changed across Android versions. */
    private class Reader {
        private val service: IInterface
        private val method: Method
        private val descMethod: Method?
        private val variant: Int

        init {
            val sm = Class.forName("android.os.ServiceManager")
            val binder = sm.getDeclaredMethod("getService", String::class.java).invoke(null, "clipboard") as IBinder
            val stub = Class.forName("android.content.IClipboard\$Stub")
            service = stub.getMethod("asInterface", IBinder::class.java).invoke(null, binder) as IInterface
            val (m, v) = lookup("getPrimaryClip")
            method = m ?: throw NoSuchMethodException("IClipboard.getPrimaryClip: " +
                service.javaClass.methods.filter { it.name == "getPrimaryClip" }.joinToString { it.toString() })
            variant = v
            descMethod = lookup("getPrimaryClipDescription").takeIf { it.second == v }?.first
        }

        private fun lookup(name: String): Pair<Method?, Int> {
            val candidates = service.javaClass.methods.filter { it.name == name }
            fun find(vararg types: Class<*>) = candidates.firstOrNull { it.parameterTypes.contentEquals(arrayOf(*types)) }
            val s = String::class.java; val i = Int::class.javaPrimitiveType!!; val b = Boolean::class.javaPrimitiveType!!
            // Newest first.
            find(s, s, s, s, i, i, b)?.let { return it to 4 }              // some vendor builds (scrcpy: MIUI/HyperOS)
            find(s, s, i, i)?.let { return it to 3 }                       // 14+: pkg, attributionTag, userId, deviceId
            find(s, s, i)?.let { return it to 2 }                          // 12/13: pkg, attributionTag, userId
            find(s, i)?.let { return it to 1 }                             // 10/11: pkg, userId
            find(s)?.let { return it to 0 }                                // <= 9: pkg
            return null to -1
        }

        private fun call(m: Method): Any? = when (variant) {
            0 -> m.invoke(service, PKG)
            1 -> m.invoke(service, PKG, 0)
            2 -> m.invoke(service, PKG, null, 0)
            3 -> m.invoke(service, PKG, null, 0, 0)
            else -> m.invoke(service, PKG, null, null, null, 0, 0, true)
        }

        /** Identity of the current clip (timestamp, label) without reading it; falls back to the text when the
         *  description call is unavailable. */
        fun stamp(): Pair<Long, String?>? {
            val dm = descMethod ?: return text()?.let { it.hashCode().toLong() to null }
            val d = call(dm) as android.content.ClipDescription? ?: return null
            return d.timestamp to d.label?.toString()
        }

        fun describe() = "getPrimaryClip v$variant, description ${descMethod != null}"

        fun text(): String? {
            val clip = call(method) as ClipData? ?: return null
            if (clip.itemCount == 0) return null
            val item = clip.getItemAt(0)
            return (item.text ?: item.htmlText ?: item.uri?.toString())?.toString()
        }
    }
}

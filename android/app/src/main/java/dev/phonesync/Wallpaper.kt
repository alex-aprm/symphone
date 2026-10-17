package dev.phonesync

import android.app.WallpaperManager
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Canvas
import android.graphics.drawable.BitmapDrawable
import android.graphics.drawable.Drawable
import android.os.SystemClock
import java.io.ByteArrayOutputStream
import java.security.MessageDigest

/** Home-screen wallpaper as a scaled JPEG, so the PC can draw a picture of this very phone. */
object Wallpaper {
    private const val TTL_MS = 10 * 60 * 1000L
    private class Entry(val jpeg: ByteArray, val id: String, val at: Long)
    private val cache = HashMap<Int, Entry>()
    @Volatile private var generation = 0

    fun invalidate() { synchronized(cache) { cache.clear(); generation++ } }

    /** (jpeg, id) or throws with a reason. */
    fun get(width: Int): Pair<ByteArray, String> {
        val w = width.coerceIn(32, 1600)
        synchronized(cache) {
            cache[w]?.takeIf { SystemClock.elapsedRealtime() - it.at < TTL_MS }?.let { return it.jpeg to it.id }
        }
        val src = load() ?: throw IllegalStateException("wallpaper not readable")
        val h = (src.height.toLong() * w / maxOf(1, src.width)).toInt().coerceAtLeast(1)
        val scaled = Bitmap.createScaledBitmap(src, w, h, true)
        val out = ByteArrayOutputStream()
        scaled.compress(Bitmap.CompressFormat.JPEG, 88, out)
        val bytes = out.toByteArray()
        val id = MessageDigest.getInstance("SHA-1").digest(bytes).take(8).joinToString("") { "%02x".format(it) }
        synchronized(cache) { cache[w] = Entry(bytes, id, SystemClock.elapsedRealtime()) }
        return bytes to id
    }

    private fun load(): Bitmap? {
        val wm = WallpaperManager.getInstance(Hub.ctx)
        try { wm.drawable?.let { return toBitmap(it) } } catch (e: Exception) { android.util.Log.w("Symphone", "wallpaper drawable: $e") }
        try { wm.peekDrawable()?.let { return toBitmap(it) } } catch (_: Exception) {}
        for (which in intArrayOf(WallpaperManager.FLAG_SYSTEM, WallpaperManager.FLAG_LOCK)) {
            try {
                wm.getWallpaperFile(which)?.use { pfd ->
                    BitmapFactory.decodeFileDescriptor(pfd.fileDescriptor)?.let { return it }
                }
            } catch (_: Exception) {}
        }
        try { wm.getBuiltInDrawable()?.let { return toBitmap(it) } } catch (_: Exception) {}
        return null
    }

    private fun toBitmap(d: Drawable): Bitmap? {
        if (d is BitmapDrawable && d.bitmap != null) return d.bitmap
        val w = d.intrinsicWidth.takeIf { it > 0 } ?: 1080
        val h = d.intrinsicHeight.takeIf { it > 0 } ?: 2400
        return Bitmap.createBitmap(w, h, Bitmap.Config.ARGB_8888).also { b ->
            val c = Canvas(b); d.setBounds(0, 0, w, h); d.draw(c)
        }
    }
}

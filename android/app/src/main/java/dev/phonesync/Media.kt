package dev.phonesync

import android.content.ContentResolver
import android.content.ContentUris
import android.database.ContentObserver
import android.graphics.Bitmap
import android.net.Uri
import android.os.Bundle
import android.os.Handler
import android.os.HandlerThread
import android.provider.MediaStore
import android.util.LruCache
import android.util.Size
import org.json.JSONArray
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.io.FileInputStream
import java.util.concurrent.ConcurrentHashMap

object Media {
    private val filesUri: Uri = MediaStore.Files.getContentUri(MediaStore.VOLUME_EXTERNAL)
    private val isVideo = ConcurrentHashMap<Long, Boolean>()
    private val thumbs = object : LruCache<String, ByteArray>(64 shl 20) {
        override fun sizeOf(key: String, value: ByteArray) = value.size
    }

    /** Column order of rows returned by [list]; the PC side reads rows positionally to keep payloads small. */
    val columns = listOf("id", "name", "mime", "taken", "modified", "size", "w", "h", "bucket", "path", "duration", "orientation", "gen")

    private val projection = arrayOf(
        MediaStore.MediaColumns._ID, MediaStore.MediaColumns.DISPLAY_NAME, MediaStore.MediaColumns.MIME_TYPE,
        MediaStore.MediaColumns.DATE_TAKEN, MediaStore.MediaColumns.DATE_MODIFIED, MediaStore.MediaColumns.SIZE,
        MediaStore.MediaColumns.WIDTH, MediaStore.MediaColumns.HEIGHT, MediaStore.MediaColumns.BUCKET_DISPLAY_NAME,
        MediaStore.MediaColumns.RELATIVE_PATH, MediaStore.MediaColumns.DURATION, MediaStore.MediaColumns.ORIENTATION,
        MediaStore.MediaColumns.GENERATION_MODIFIED, MediaStore.Files.FileColumns.MEDIA_TYPE,
    )

    fun generation(): Long = MediaStore.getGeneration(Hub.ctx, MediaStore.VOLUME_EXTERNAL_PRIMARY)

    fun list(offset: Int, limit: Int, sinceGen: Long): JSONObject {
        val sel = StringBuilder("${MediaStore.Files.FileColumns.MEDIA_TYPE} IN (1,3)")
        val args = ArrayList<String>()
        if (sinceGen > 0) { sel.append(" AND ${MediaStore.MediaColumns.GENERATION_MODIFIED} > ?"); args += sinceGen.toString() }
        val q = Bundle().apply {
            putString(ContentResolver.QUERY_ARG_SQL_SELECTION, sel.toString())
            putStringArray(ContentResolver.QUERY_ARG_SQL_SELECTION_ARGS, args.toTypedArray())
            putString(ContentResolver.QUERY_ARG_SQL_SORT_ORDER, "${MediaStore.MediaColumns.DATE_MODIFIED} DESC, ${MediaStore.MediaColumns._ID} DESC")
            putInt(ContentResolver.QUERY_ARG_LIMIT, limit)
            putInt(ContentResolver.QUERY_ARG_OFFSET, offset)
        }
        val rows = JSONArray()
        val gen = generation()
        Hub.ctx.contentResolver.query(filesUri, projection, q, null)?.use { c ->
            while (c.moveToNext()) {
                val id = c.getLong(0)
                val video = c.getInt(13) == MediaStore.Files.FileColumns.MEDIA_TYPE_VIDEO
                isVideo[id] = video
                val taken = if (c.isNull(3)) c.getLong(4) * 1000 else c.getLong(3)
                rows.put(JSONArray()
                    .put(id).put(c.getString(1) ?: "").put(c.getString(2) ?: "")
                    .put(taken).put(c.getLong(4)).put(c.getLong(5))
                    .put(c.getInt(6)).put(c.getInt(7)).put(c.getString(8) ?: "")
                    .put(c.getString(9) ?: "").put(c.getLong(10)).put(c.getInt(11)).put(c.getLong(12)))
            }
        }
        return JSONObject().put("columns", JSONArray(columns)).put("rows", rows).put("gen", gen)
    }

    /** All media ids, for detecting deletions cheaply. */
    fun ids(): JSONArray {
        val out = JSONArray()
        Hub.ctx.contentResolver.query(filesUri, arrayOf(MediaStore.MediaColumns._ID),
            "${MediaStore.Files.FileColumns.MEDIA_TYPE} IN (1,3)", null, null)?.use { c ->
            while (c.moveToNext()) out.put(c.getLong(0))
        }
        return out
    }

    fun uriFor(id: Long): Uri {
        val video = isVideo[id] ?: run {
            Hub.ctx.contentResolver.query(ContentUris.withAppendedId(filesUri, id),
                arrayOf(MediaStore.Files.FileColumns.MEDIA_TYPE), null, null, null)?.use { c ->
                if (c.moveToFirst()) c.getInt(0) == MediaStore.Files.FileColumns.MEDIA_TYPE_VIDEO else null
            } ?: false
        }.also { isVideo[id] = it }
        val base = if (video) MediaStore.Video.Media.EXTERNAL_CONTENT_URI else MediaStore.Images.Media.EXTERNAL_CONTENT_URI
        return ContentUris.withAppendedId(base, id)
    }

    fun thumb(id: Long, size: Int): ByteArray {
        val key = "$id:$size"
        thumbs.get(key)?.let { return it }
        val bmp = Hub.ctx.contentResolver.loadThumbnail(uriFor(id), Size(size, size), null)
        val out = ByteArrayOutputStream(32 * 1024)
        bmp.compress(Bitmap.CompressFormat.JPEG, if (size <= 400) 80 else 88, out)
        bmp.recycle()
        return out.toByteArray().also { thumbs.put(key, it) }
    }

    fun serveOriginal(req: Request, resp: Response, id: Long) {
        val uri = uriFor(id)
        val cr = Hub.ctx.contentResolver
        var name = "file"; var mime = "application/octet-stream"; var mtime = 0L
        cr.query(uri, arrayOf(MediaStore.MediaColumns.DISPLAY_NAME, MediaStore.MediaColumns.MIME_TYPE, MediaStore.MediaColumns.DATE_MODIFIED), null, null, null)?.use { c ->
            if (c.moveToFirst()) { name = c.getString(0) ?: name; mime = c.getString(1) ?: mime; mtime = c.getLong(2) * 1000 }
        }
        val pfd = cr.openFileDescriptor(uri, "r") ?: return resp.text(404, "not found")
        pfd.use {
            FileInputStream(it.fileDescriptor).channel.use { fc ->
                val disp = if (req.query.containsKey("dl")) "attachment" else "inline"
                resp.file(fc, fc.size(), mime, mtime, listOf("Content-Disposition" to "$disp; filename*=UTF-8''${Http.encodePath(name)}"))
            }
        }
    }

    // ---- change notifications ----
    private var observer: ContentObserver? = null

    fun watch() {
        if (observer != null) return
        val t = HandlerThread("media-watch").apply { start() }
        val h = Handler(t.looper)
        val fire = Runnable { Hub.emit("media", JSONObject().put("gen", generation())) }
        observer = object : ContentObserver(h) {
            override fun onChange(selfChange: Boolean, uri: Uri?) {
                h.removeCallbacks(fire)
                h.postDelayed(fire, 250)
            }
        }
        Hub.ctx.contentResolver.registerContentObserver(filesUri, true, observer!!)
    }
}

package dev.phonesync

import android.app.KeyguardManager
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioTrack
import android.net.wifi.WifiManager
import android.os.BatteryManager
import android.os.Handler
import android.os.Looper
import android.os.PowerManager
import android.os.StatFs
import android.telecom.TelecomManager
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.io.IOException
import java.nio.ByteBuffer
import java.nio.channels.FileChannel
import java.nio.file.StandardOpenOption
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.TimeUnit

object Api {
    private val main = Handler(Looper.getMainLooper())
    private val appIcons = ConcurrentHashMap<String, ByteArray>()

    fun handle(req: Request, resp: Response, path: String) {
        val seg = path.removePrefix("/api/").split('/')
        val me = req.pc!!
        when (seg[0]) {
            "info" -> resp.json(info(me))
            "events" -> events(me, req, resp)
            "pc" -> pcApi(req, resp, me, seg.getOrNull(1) ?: "")
            "clip" -> if (seg.getOrNull(1) == "shell") {
                val text = try { JSONObject(req.bodyText()).optString("text") } catch (_: Exception) { "" }
                val sent = Hub.phoneClipChanged(text, "shell")
                resp.json(JSONObject().put("ok", true).put("sent", sent))
            } else resp.status(404)
            "ls" -> ls(req, resp)
            "media" -> when {
                seg.size > 1 && seg[1] == "ids" -> resp.json(JSONObject().put("ids", Media.ids()).put("gen", Media.generation()))
                seg.size > 1 -> Media.serveOriginal(req, resp, seg[1].toLong())
                else -> resp.json(Media.list(
                    req.query["offset"]?.toIntOrNull() ?: 0,
                    req.query["limit"]?.toIntOrNull() ?: 2000,
                    req.query["gen"]?.toLongOrNull() ?: 0))
            }
            "thumb" -> {
                val id = seg.getOrNull(1)?.toLongOrNull() ?: return resp.status(400)
                val size = (req.query["s"]?.toIntOrNull() ?: 256).coerceIn(64, 1600)
                val data = try { Media.thumb(id, size) } catch (e: Exception) { return resp.text(404, e.toString()) }
                resp.bytes(200, data, "image/jpeg", listOf("Cache-Control" to "max-age=31536000, immutable"))
            }
            "notifications" -> resp.json(JSONObject().put("items", NotifListener.list()).put("enabled", NotifListener.instance != null))
            "notif" -> notif(req, resp, seg)
            "apps" -> resp.json(apps())
            "icon" -> appIcon(resp, seg.getOrNull(1) ?: "")
            "launch" -> {
                val b = JSONObject(req.bodyText())
                val pkg = b.optString("pkg")
                val display = displayParam(b)
                val i = Hub.ctx.packageManager.getLaunchIntentForPackage(pkg) ?: return resp.text(404, "no launcher")
                i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_RESET_TASK_IF_NEEDED)
                main.post {
                    try {
                        if (display != null) Hub.ctx.startActivity(i, NotifListener.launchOptions(display))
                        else Hub.ctx.startActivity(i)
                    } catch (e: Exception) {
                        android.util.Log.w("Symphone", "launch on display $display: $e")
                        try { Hub.ctx.startActivity(i, NotifListener.launchOptions(null)) } catch (e2: Exception) { android.util.Log.w("Symphone", "launch: $e2") }
                    }
                }
                resp.json(JSONObject().put("ok", true))
            }
            "capture" -> {
                val facing = try { JSONObject(req.bodyText()).optString("facing", "back") } catch (_: Exception) { "back" }
                val i = Intent(Hub.ctx, CaptureActivity::class.java)
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_NO_ANIMATION)
                    .putExtra("facing", facing)
                main.post { Hub.ctx.startActivity(i) }
                resp.json(JSONObject().put("ok", true))
            }
            "shares" -> resp.json(JSONObject().put("items", Shares.list(me)))
            "share" -> share(req, resp, seg.getOrNull(1) ?: "")
            "audio" -> AudioSink.play(req, resp)
            "call" -> call(req, resp)
            "clipboard" -> {
                val text = JSONObject(req.bodyText()).optString("text")
                Hub.lastClipFromPc = text
                main.post {
                    (Hub.ctx.getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager)
                        .setPrimaryClip(ClipData.newPlainText(ShellClip.PC_LABEL, text))
                }
                resp.json(JSONObject().put("ok", true))
            }
            "ime" -> when (seg.getOrNull(1)) {
                "lease" -> {
                    val b = try { JSONObject(req.bodyText()) } catch (_: Exception) { JSONObject() }
                    val arr = b.optJSONArray("pkgs")
                    val pkgs = HashSet<String>()
                    if (arr != null) for (i in 0 until arr.length()) arr.optString(i).takeIf { it.isNotEmpty() }?.let { pkgs.add(it) }
                    val prev = if (b.isNull("prev")) null else b.optString("prev").takeIf { it.isNotEmpty() }
                    PcKeyboard.Lease.set(pkgs, b.optBoolean("any", false), prev, b.optInt("ttl", 20))
                    resp.json(PcKeyboard.status())
                }
                "release" -> { PcKeyboard.release(); resp.json(JSONObject().put("ok", true)) }
                "input" -> {
                    val b = try { JSONObject(req.bodyText()) } catch (_: Exception) { JSONObject() }
                    resp.json(PcKeyboard.input(b))
                }
                "status" -> resp.json(PcKeyboard.status())
                else -> resp.text(404, "unknown api")
            }
            "wallpaper" -> {
                val w = req.query["w"]?.toIntOrNull() ?: 480
                try {
                    val (jpeg, id) = Wallpaper.get(w)
                    resp.bytes(200, jpeg, "image/jpeg", listOf("X-Wallpaper-Id" to id, "Cache-Control" to "no-cache"))
                } catch (e: Exception) {
                    resp.json(JSONObject().put("error", e.message ?: e.toString()), 404)
                }
            }
            "cameras" -> resp.json(JSONObject().put("cameras", Cameras.list()))
            "camera" -> when (seg.getOrNull(1)) {
                "orientation" -> resp.json(Cameras.orientation(req.query["id"]))
                else -> resp.text(404, "unknown api")
            }
            "torch" -> {
                if (req.method == "POST") Torch.set(JSONObject(req.bodyText()).optBoolean("on"))
                resp.json(JSONObject().put("on", Torch.on))
            }
            "upload" -> upload(req, resp)
            "chunk" -> chunk(req, resp)
            "commit" -> commit(req, resp)
            else -> resp.text(404, "unknown api")
        }
    }

    /** Optional "display" (int >= 0) of /api/notif/act and /api/launch. */
    private fun displayParam(b: JSONObject): Int? =
        if (b.has("display") && !b.isNull("display")) b.optInt("display", -1).takeIf { it >= 0 } else null

    fun info(me: Pc?): JSONObject {
        val ctx = Hub.ctx
        val bat = ctx.registerReceiver(null, IntentFilter(Intent.ACTION_BATTERY_CHANGED))
        val level = bat?.let { it.getIntExtra(BatteryManager.EXTRA_LEVEL, -1) * 100 / maxOf(1, it.getIntExtra(BatteryManager.EXTRA_SCALE, 100)) } ?: -1
        val plugged = (bat?.getIntExtra(BatteryManager.EXTRA_PLUGGED, 0) ?: 0) != 0
        val st = StatFs(WebDav.root.path)
        val pm = ctx.getSystemService(Context.POWER_SERVICE) as PowerManager
        val km = ctx.getSystemService(Context.KEYGUARD_SERVICE) as KeyguardManager
        return Hub.pingJson()
            .put("battery", level).put("charging", plugged)
            .put("storageFree", st.availableBytes).put("storageTotal", st.totalBytes)
            .put("screenOn", pm.isInteractive).put("locked", km.isKeyguardLocked)
            .put("wifiIp", Discovery.wifiIp() ?: "")
            .put("wfdIp", WfdClient.network?.let { WfdClient.myIp(it) } ?: "")
            .put("staConcurrency", WfdClient.concurrent)
            .put("notifAccess", NotifListener.instance != null)
            .put("allFiles", android.os.Environment.isExternalStorageManager())
            .put("mediaGen", Media.generation())
            .put("torch", Torch.on)
            .put("you", me?.id ?: "")
            .put("master", Pcs.master?.id ?: "")
            .put("pcs", Pcs.publicList())
            .put("clipShare", Hub.clipShare)
    }

    /** /api/pc[/master|/wfd]: PC registry operations, on behalf of the requesting PC. */
    private fun pcApi(req: Request, resp: Response, me: Pc, sub: String) {
        val body = if (req.method == "POST" || req.method == "PUT")
            try { JSONObject(req.bodyText()) } catch (_: Exception) { JSONObject() } else JSONObject()
        when {
            sub == "" && req.method == "GET" -> resp.json(Pcs.eventJson().put("you", me.id))
            sub == "" && req.method == "POST" -> {
                Pcs.register(me, body.optString("id").ifBlank { null }, body.optString("name").ifBlank { null })
                resp.json(JSONObject().put("ok", true).put("id", me.id).put("name", me.name).put("master", me.master))
            }
            sub == "" && req.method == "DELETE" -> {
                // Answer first: removing the entry also closes this PC's event streams.
                resp.json(JSONObject().put("ok", true))
                Pcs.remove(me.id)
            }
            sub == "master" && req.method == "POST" -> {
                val id = body.optString("id").ifBlank { me.id }
                if (!Pcs.setMaster(id)) return resp.json(JSONObject().put("ok", false).put("error", "unknown pc"), 404)
                resp.json(JSONObject().put("ok", true).put("master", id))
            }
            sub == "wfd" && req.method == "POST" -> {
                val ssid = body.optString("ssid")
                Pcs.setWfd(me, if (ssid.isEmpty()) null else Wfd(ssid, body.optString("pass"), body.optBoolean("prefer")))
                WfdClient.reevaluate()
                resp.json(JSONObject().put("ok", true))
            }
            sub == "wfd" && req.method == "DELETE" -> { Pcs.setWfd(me, null); resp.json(JSONObject().put("ok", true)) }
            else -> resp.status(405)
        }
    }

    private fun events(me: Pc, req: Request, resp: Response) {
        val sub = Hub.subscribe(me.id)
        // The client never sends anything on an event stream: a read returning EOF means it went away. Notice that
        // immediately (instead of at the next keep-alive write) so online state and event routing stay accurate.
        Thread({
            try {
                val bb = java.nio.ByteBuffer.allocate(256)
                while (!sub.closed) { bb.clear(); if (req.conn.sc.read(bb) < 0) break }
            } catch (_: Exception) {}
            if (!sub.closed) { sub.closed = true; sub.queue.offer("") }
        }, "sse-watch").apply { isDaemon = true }.start()
        try {
            resp.begin(200, listOf("Content-Type" to "text/event-stream", "Cache-Control" to "no-cache", "X-Accel-Buffering" to "no"), null)
            resp.write("retry: 1000\nevent: hello\ndata: ${info(me)}\n\n".toByteArray())
            while (!sub.closed) {
                val m = sub.queue.poll(15, TimeUnit.SECONDS)
                if (sub.closed) break
                if (m == "") continue
                if (m == null) { resp.write(": ping\n\n".toByteArray()); Pcs.seen(me, null, request = false); continue }
                val sb = StringBuilder(m)
                while (true) { sb.append(sub.queue.poll() ?: break) }
                resp.write(sb.toString().toByteArray())
            }
        } finally {
            Hub.unsubscribe(sub)
        }
    }

    private fun ls(req: Request, resp: Response) {
        val f = WebDav.resolve(req.query["path"] ?: "/") ?: return resp.status(400)
        val arr = JSONArray()
        f.listFiles()?.forEach {
            arr.put(JSONObject().put("n", it.name).put("d", it.isDirectory).put("s", it.length()).put("m", it.lastModified()))
        } ?: return resp.text(404, "not a directory")
        resp.json(JSONObject().put("path", req.query["path"] ?: "/").put("items", arr))
    }

    /** PUT /api/upload?path=/Download/x.bin  — creates parent dirs, writes atomically via .part file. */
    private fun upload(req: Request, resp: Response) {
        val f = WebDav.resolve(req.query["path"] ?: return resp.status(400)) ?: return resp.status(400)
        f.parentFile?.mkdirs()
        val tmp = File(f.parentFile, ".${f.name}.part")
        val n = FileChannel.open(tmp.toPath(), StandardOpenOption.CREATE, StandardOpenOption.WRITE, StandardOpenOption.TRUNCATE_EXISTING).use { req.bodyTo(it) }
        req.query["mtime"]?.toLongOrNull()?.let { tmp.setLastModified(it) }
        if (f.exists()) f.delete()
        if (!tmp.renameTo(f)) return resp.text(500, "rename failed")
        WebDav.scan(f)
        resp.json(JSONObject().put("ok", true).put("size", n))
    }

    private fun partFile(f: File, id: String) = File(f.parentFile, ".${f.name}.$id.part")

    /**
     * PUT /api/chunk?path=&id=&offset=&total=  — one slice of a multipath upload. Slices may arrive
     * out of order over different links; each is written at its offset into a shared .part file.
     */
    private fun chunk(req: Request, resp: Response) {
        val f = WebDav.resolve(req.query["path"] ?: return resp.status(400)) ?: return resp.status(400)
        val id = req.query["id"]?.filter { it.isLetterOrDigit() } ?: return resp.status(400)
        val offset = req.query["offset"]?.toLongOrNull() ?: return resp.status(400)
        f.parentFile?.mkdirs()
        val part = partFile(f, id)
        FileChannel.open(part.toPath(), StandardOpenOption.CREATE, StandardOpenOption.WRITE).use { fc ->
            fc.position(offset)
            req.bodyTo(fc)
        }
        resp.status(204)
    }

    /** POST /api/commit?path=&id=&total=&mtime= — finalises a multipath upload. */
    private fun commit(req: Request, resp: Response) {
        val f = WebDav.resolve(req.query["path"] ?: return resp.status(400)) ?: return resp.status(400)
        val id = req.query["id"]?.filter { it.isLetterOrDigit() } ?: return resp.status(400)
        val part = partFile(f, id)
        if (!part.exists()) {
            if (req.query["total"] == "0") part.createNewFile() else return resp.text(404, "no such upload")
        }
        req.query["total"]?.toLongOrNull()?.let { total ->
            if (part.length() != total) {
                FileChannel.open(part.toPath(), StandardOpenOption.WRITE).use { if (it.size() > total) it.truncate(total) }
                if (part.length() != total) return resp.text(409, "size mismatch ${part.length()} != $total")
            }
        }
        req.query["mtime"]?.toLongOrNull()?.let { part.setLastModified(it) }
        if (f.exists()) f.deleteRecursively()
        if (!part.renameTo(f)) return resp.text(500, "rename failed")
        WebDav.scan(f)
        resp.json(JSONObject().put("ok", true).put("size", f.length()))
    }

    private fun notif(req: Request, resp: Response, seg: List<String>) {
        when (seg.getOrNull(1)) {
            "icon" -> {
                val png = NotifListener.largeIcon(req.query["key"] ?: "") ?: return resp.status(404)
                resp.bytes(200, png, "image/png", listOf("Cache-Control" to "max-age=3600"))
            }
            "act" -> {
                val b = JSONObject(req.bodyText())
                val err = NotifListener.act(b.optString("key"), b.optString("op"), b.optInt("index", -1), b.optString("text", null), displayParam(b))
                resp.json(JSONObject().put("ok", err == null).put("error", err ?: ""), if (err == null) 200 else 409)
            }
            else -> resp.status(404)
        }
    }

    private fun apps(): JSONObject {
        val pm = Hub.ctx.packageManager
        val i = Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER)
        val arr = JSONArray()
        pm.queryIntentActivities(i, PackageManager.ResolveInfoFlags.of(0))
            .distinctBy { it.activityInfo.packageName }
            .map { it.activityInfo.packageName to it.loadLabel(pm).toString() }
            .sortedBy { it.second.lowercase() }
            .forEach { (pkg, label) ->
                val sys = try { (pm.getApplicationInfo(pkg, 0).flags and android.content.pm.ApplicationInfo.FLAG_SYSTEM) != 0 } catch (_: Exception) { false }
                arr.put(JSONObject().put("pkg", pkg).put("label", label).put("system", sys))
            }
        return JSONObject().put("items", arr)
    }

    private fun appIcon(resp: Response, pkg: String) {
        val png = appIcons[pkg] ?: try {
            NotifListener.png(Hub.ctx.packageManager.getApplicationIcon(pkg), 128).also { appIcons[pkg] = it }
        } catch (_: Exception) { return resp.status(404) }
        resp.bytes(200, png, "image/png", listOf("Cache-Control" to "max-age=86400"))
    }

    private fun share(req: Request, resp: Response, id: String) {
        val s = Shares.get(id) ?: return resp.status(404)
        if (req.method == "DELETE") { Shares.remove(id); return resp.status(204) }
        FileChannel.open(s.file.toPath(), StandardOpenOption.READ).use {
            resp.file(it, it.size(), s.mime, s.file.lastModified(),
                listOf("Content-Disposition" to "attachment; filename*=UTF-8''${Http.encodePath(s.name)}"))
        }
    }

    private fun call(req: Request, resp: Response) {
        val op = JSONObject(req.bodyText()).optString("op")
        val tm = Hub.ctx.getSystemService(Context.TELECOM_SERVICE) as TelecomManager
        try {
            @Suppress("DEPRECATION", "MissingPermission")
            when (op) {
                "answer" -> tm.acceptRingingCall()
                "end" -> tm.endCall()
                "silence" -> tm.silenceRinger()
                else -> return resp.status(400)
            }
        } catch (e: SecurityException) {
            return resp.json(JSONObject().put("ok", false).put("error", "phone permission missing"), 403)
        }
        resp.json(JSONObject().put("ok", true))
    }
}

/** Plays a raw PCM stream from the PC through the phone speaker ("phone as PC speaker"). */
object AudioSink {
    @Volatile private var current: AudioTrack? = null

    fun play(req: Request, resp: Response) {
        val rate = req.query["rate"]?.toIntOrNull() ?: 48000
        val ch = req.query["ch"]?.toIntOrNull() ?: 2
        val chMask = if (ch == 1) AudioFormat.CHANNEL_OUT_MONO else AudioFormat.CHANNEL_OUT_STEREO
        val min = AudioTrack.getMinBufferSize(rate, chMask, AudioFormat.ENCODING_PCM_16BIT)
        val track = AudioTrack.Builder()
            .setAudioAttributes(AudioAttributes.Builder()
                .setUsage(AudioAttributes.USAGE_MEDIA).setContentType(AudioAttributes.CONTENT_TYPE_MUSIC).build())
            .setAudioFormat(AudioFormat.Builder().setSampleRate(rate).setChannelMask(chMask)
                .setEncoding(AudioFormat.ENCODING_PCM_16BIT).build())
            .setBufferSizeInBytes(min * 2)
            .setPerformanceMode(AudioTrack.PERFORMANCE_MODE_LOW_LATENCY)
            .setTransferMode(AudioTrack.MODE_STREAM)
            .build()
        current?.let { try { it.stop() } catch (_: Exception) {} }
        current = track
        track.play()
        val bb = ByteBuffer.allocateDirect(8192)
        try {
            while (current === track) {
                bb.clear()
                val n = req.readBody(bb)
                if (n < 0) break
                bb.flip()
                track.write(bb, bb.remaining(), AudioTrack.WRITE_BLOCKING)
            }
        } catch (_: IOException) {
        } finally {
            try { track.stop() } catch (_: Exception) {}
            track.release()
            if (current === track) current = null
        }
        resp.status(204)
    }
}

/** [pc] = explicit target PC (direct-share shortcut); null = whoever is master. */
class Share(val id: String, val name: String, val mime: String, val file: File, val pc: String? = null) {
    /** Effective target: the explicit PC if it is still paired, else the master. */
    fun target(): String? = pc?.takeIf { Pcs.byId(it) != null } ?: Pcs.master?.id
    fun json() = JSONObject().put("id", id).put("name", name).put("mime", mime).put("size", file.length())
        .put("pc", target() ?: "")
}

/** Files shared to "PC" via the Android share sheet, queued until the PC fetches them. */
object Shares {
    private val items = ConcurrentHashMap<String, Share>()
    val dir: File get() = File(Hub.ctx.cacheDir, "shares").also { it.mkdirs() }

    private val safePc = Regex("[A-Za-z0-9-]{1,64}")

    fun add(s: Share) { items[s.id] = s; Hub.emitTargeted("share", s.json(), s.pc) }
    fun get(id: String) = items[id]
    fun remove(id: String) { items.remove(id)?.file?.delete() }

    /** Staging file name: `<id>[~<pc>]_<name>` so the target survives a restart. */
    fun fileFor(id: String, name: String, pc: String?): File {
        val tag = pc?.takeIf { safePc.matches(it) }?.let { "~$it" } ?: ""
        return File(dir, "$id${tag}_${name.replace('/', '_')}")
    }

    /** Shares meant for [me]: its own, plus the master's while the master is not connected. */
    fun visibleTo(s: Share, me: Pc): Boolean {
        val t = s.target() ?: return true
        if (t == me.id) return true
        val m = Pcs.master?.id
        return t == m && !Hub.hasSubscriber(m)
    }

    fun list(me: Pc) = JSONArray().also { a ->
        items.values.sortedBy { it.id }.filter { visibleTo(it, me) }.forEach { a.put(it.json()) }
    }

    fun restore() {
        dir.listFiles()?.forEach { f ->
            val head = f.name.substringBefore('_')
            val id = head.substringBefore('~')
            val pc = head.substringAfter('~', "").ifEmpty { null }
            val name = f.name.substringAfter('_')
            if (!items.containsKey(id)) items[id] = Share(id, name, Http.mime(name), f, pc)
        }
    }
}

/** Flashlight via CameraManager; state tracked with a TorchCallback so phone-side toggles are reflected too. */
object Torch {
    @Volatile var on = false
        private set
    private var cameraId: String? = null
    private val cm by lazy { Hub.ctx.getSystemService(Context.CAMERA_SERVICE) as android.hardware.camera2.CameraManager }

    fun init() {
        if (cameraId != null) return
        cameraId = cm.cameraIdList.firstOrNull {
            cm.getCameraCharacteristics(it).get(android.hardware.camera2.CameraCharacteristics.FLASH_INFO_AVAILABLE) == true
        }
        cm.registerTorchCallback(object : android.hardware.camera2.CameraManager.TorchCallback() {
            override fun onTorchModeChanged(id: String, enabled: Boolean) { if (id == cameraId) on = enabled }
        }, Handler(Looper.getMainLooper()))
    }

    fun set(enable: Boolean) {
        init()
        cameraId?.let { cm.setTorchMode(it, enable); on = enable }
    }
}

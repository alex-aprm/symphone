package dev.phonesync

import android.Manifest
import android.annotation.SuppressLint
import android.app.Activity
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.graphics.Rect
import android.hardware.camera2.CameraCaptureSession
import android.hardware.camera2.CameraCharacteristics
import android.hardware.camera2.CameraDevice
import android.hardware.camera2.CameraManager
import android.hardware.camera2.CaptureRequest
import android.hardware.display.DisplayManager
import android.hardware.display.VirtualDisplay
import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioPlaybackCaptureConfiguration
import android.media.AudioRecord
import android.media.MediaCodec
import android.media.MediaCodecInfo
import android.media.MediaFormat
import android.media.MediaRecorder
import android.media.projection.MediaProjection
import android.media.projection.MediaProjectionManager
import android.net.wifi.WifiManager
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.HandlerThread
import android.os.IBinder
import android.os.Looper
import android.provider.Settings
import android.util.Log
import android.util.Range
import android.util.Size
import android.view.Display
import android.view.Surface
import android.view.WindowManager
import org.json.JSONObject
import java.io.BufferedOutputStream
import java.io.DataOutputStream
import java.io.InputStream
import java.net.ServerSocket
import java.net.Socket
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import kotlin.concurrent.thread

/**
 * Debug-free media streams for the PC (docs/protocol-v2.md "Debug-free mode"): TCP 47904, one connection per stream.
 *  screen  MediaProjection of the phone's display -> H.264 (the user allows casting on the phone each session)
 *  camera  Camera2 -> H.264
 *  audio   "playback" (AudioPlaybackCapture, needs the casting consent too) or "mic" -> PCM s16le 48 kHz stereo
 * Input lines from the PC on a screen stream go to the accessibility service (ControlService).
 */
object StreamServer {
    const val PORT = 47904
    private const val TAG = "SymphoneStream"
    @Volatile private var server: ServerSocket? = null

    fun start() {
        if (server != null) return
        thread(name = "stream-accept", isDaemon = true) {
            try {
                val ss = ServerSocket(PORT)
                server = ss
                while (true) {
                    val s = ss.accept()
                    thread(name = "stream", isDaemon = true) { handle(s) }
                }
            } catch (e: Exception) { Log.e(TAG, "server: $e"); server = null }
        }
    }

    private fun readLine(ins: InputStream, max: Int = 8192): String? {
        val b = java.io.ByteArrayOutputStream()
        while (b.size() < max) {
            val c = ins.read()
            if (c < 0) return if (b.size() > 0) b.toString("UTF-8") else null
            if (c == '\n'.code) return b.toString("UTF-8")
            b.write(c)
        }
        return null
    }

    private fun handle(s: Socket) {
        s.tcpNoDelay = true
        val out = Out(s)
        var stream: Stream? = null
        try {
            val ins = s.getInputStream()
            s.soTimeout = 10_000
            val hello = JSONObject(readLine(ins) ?: return)
            val pc = Pcs.byToken(hello.optString("token"))
            if (pc == null) { out.line(JSONObject().put("ok", false).put("error", "bad token")); return }
            s.soTimeout = 0
            stream = when (hello.optString("kind")) {
                "screen" -> ScreenStream(hello, out)
                "camera" -> CameraStream(hello, out)
                "audio" -> AudioStream(hello, out)
                else -> { out.line(JSONObject().put("ok", false).put("error", "unknown kind")); return }
            }
            val res = stream.start()
            out.line(res)
            if (!res.optBoolean("ok")) return
            // PC -> phone: input lines until the PC closes
            while (true) {
                val line = readLine(ins) ?: break
                if (line.isBlank()) continue
                try { stream.input(JSONObject(line)) } catch (e: Exception) { Log.d(TAG, "input: $e") }
            }
        } catch (e: Exception) {
            Log.i(TAG, "stream: $e")
        } finally {
            try { stream?.stop() } catch (_: Exception) {}
            out.ended("closed")
            try { s.close() } catch (_: Exception) {}
        }
    }

    /** Thread-safe writer for the binary packet stream. */
    class Out(private val s: Socket) {
        init {
            // A small send buffer: when Wi-Fi stalls, writes block and the encoder skips frames instead of the
            // kernel queueing seconds of stale video that would then arrive late (~150 ms at 8 Mbit/s).
            try { s.sendBufferSize = 160 * 1024 } catch (_: Exception) {}
        }
        private val o = DataOutputStream(BufferedOutputStream(s.getOutputStream(), 1 shl 16))
        @Volatile var closed = false
        private var ended = false
        private var frame = ByteArray(1 shl 16)      // reused: no garbage per frame

        @Synchronized fun line(j: JSONObject) { o.write((j.toString() + "\n").toByteArray()); o.flush() }

        @Synchronized fun video(config: Boolean, key: Boolean, ptsUs: Long, buf: java.nio.ByteBuffer, len: Int) {
            if (closed) return
            try {
                o.writeByte(0x01); o.writeByte((if (config) 1 else 0) or (if (key) 2 else 0)); o.writeLong(ptsUs); o.writeInt(len)
                if (frame.size < len) frame = ByteArray(maxOf(len, frame.size * 2))
                buf.get(frame, 0, len); o.write(frame, 0, len); o.flush()
            } catch (e: Exception) { closed = true; try { s.close() } catch (_: Exception) {} }
        }

        @Synchronized fun audio(ptsUs: Long, a: ByteArray, len: Int) {
            if (closed) return
            try { o.writeByte(0x02); o.writeLong(ptsUs); o.writeInt(len); o.write(a, 0, len); o.flush() }
            catch (e: Exception) { closed = true; try { s.close() } catch (_: Exception) {} }
        }

        @Synchronized fun event(j: JSONObject) {
            if (closed) return
            try { val b = j.toString().toByteArray(); o.writeByte(0x10); o.writeInt(b.size); o.write(b); o.flush() }
            catch (e: Exception) { closed = true }
        }

        @Synchronized fun ended(reason: String) {
            if (ended) return
            ended = true
            event(JSONObject().put("type", "ended").put("reason", reason))
            closed = true
            try { s.shutdownOutput() } catch (_: Exception) {}
        }
    }

    interface Stream {
        fun start(): JSONObject
        fun input(m: JSONObject)
        fun stop()
    }
}

/**
 * Wi-Fi without power save while the PC streams video over it (leased and renewed by the PC: POST /api/stream).
 * The low-latency lock [SyncService] holds only works with the screen on and Symphone in the foreground; in power
 * save the phone's radio dozes between beacons and frames reach the PC in bursts, 100 ms and more late.
 */
object WifiPerf {
    private val h = Handler(Looper.getMainLooper())
    private val expire = Runnable { set(false) }
    private var lock: WifiManager.WifiLock? = null

    @Synchronized fun lease(ttlSec: Int) {
        h.removeCallbacks(expire)
        set(ttlSec > 0)
        if (ttlSec > 0) h.postDelayed(expire, ttlSec * 1000L)
    }

    @Synchronized private fun set(on: Boolean) {
        if (on == (lock != null)) return
        if (on) {
            val wm = Hub.ctx.applicationContext.getSystemService(WifiManager::class.java)
            @Suppress("DEPRECATION")
            lock = wm.createWifiLock(WifiManager.WIFI_MODE_FULL_HIGH_PERF, "symphone-stream").apply {
                setReferenceCounted(false); acquire()
            }
        } else {
            try { lock?.release() } catch (_: Exception) {}
            lock = null
        }
        Log.i("SymphoneStream", "wifi high-perf ${if (on) "on" else "off"}")
    }
}

/** H.264 or H.265 encoder fed through an input surface; packets go to [out]. [hevc]: H.265 if the phone can encode
 *  it (about the same picture for less data than H.264, so less Wi-Fi airtime); [name] says what it ended up being. */
class VideoEncoder(w: Int, h: Int, fps: Int, bitrate: Int, private val out: StreamServer.Out, hevc: Boolean = false) {
    val codec: MediaCodec
    val name: String
    val surface: Surface
    @Volatile private var running = true
    private val drainer: Thread

    init {
        var c: MediaCodec? = null
        if (hevc) {
            try {
                c = MediaCodec.createEncoderByType(MediaFormat.MIMETYPE_VIDEO_HEVC)
                c.configure(format(MediaFormat.MIMETYPE_VIDEO_HEVC, w, h, fps, bitrate).apply {
                    setInteger(MediaFormat.KEY_PROFILE, MediaCodecInfo.CodecProfileLevel.HEVCProfileMain)
                }, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE)
            } catch (e: Exception) {
                Log.i("SymphoneStream", "no H.265 encoder ($e): H.264")
                try { c?.release() } catch (_: Exception) {}
                c = null
            }
        }
        if (c == null) {
            c = MediaCodec.createEncoderByType(MediaFormat.MIMETYPE_VIDEO_AVC)
            val f = format(MediaFormat.MIMETYPE_VIDEO_AVC, w, h, fps, bitrate).apply {
                setInteger(MediaFormat.KEY_PROFILE, MediaCodecInfo.CodecProfileLevel.AVCProfileHigh)
                setInteger(MediaFormat.KEY_LEVEL, MediaCodecInfo.CodecProfileLevel.AVCLevel42)
            }
            try {
                c.configure(f, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE)
            } catch (e: Exception) {                 // profile/level not supported: plain baseline settings
                f.removeKey(MediaFormat.KEY_PROFILE); f.removeKey(MediaFormat.KEY_LEVEL)
                c.configure(f, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE)
            }
            name = "h264"
        } else name = "h265"
        codec = c
        surface = codec.createInputSurface()
        codec.start()
        drainer = thread(name = "enc-drain", isDaemon = true) { drain() }
    }

    private fun format(mime: String, w: Int, h: Int, fps: Int, bitrate: Int) = MediaFormat.createVideoFormat(mime, w, h).apply {
        setInteger(MediaFormat.KEY_COLOR_FORMAT, MediaCodecInfo.CodecCapabilities.COLOR_FormatSurface)
        setInteger(MediaFormat.KEY_BIT_RATE, bitrate)
        setInteger(MediaFormat.KEY_FRAME_RATE, fps)
        // The display composes at the panel's rate (120-144 Hz): without a cap the encoder gets every composition,
        // so the bitrate is spread over twice the frames and the viewer gets more frames than it can show.
        setFloat("max-fps-to-encoder", fps.toFloat())
        setInteger(MediaFormat.KEY_I_FRAME_INTERVAL, 10)
        setLong(MediaFormat.KEY_REPEAT_PREVIOUS_FRAME_AFTER, 100_000)       // static screens keep a frame flow
        setInteger(MediaFormat.KEY_PRIORITY, 0)
        if (Build.VERSION.SDK_INT >= 30) setInteger(MediaFormat.KEY_LOW_LATENCY, 1)
    }

    private fun drain() {
        val info = MediaCodec.BufferInfo()
        while (running && !out.closed) {
            val i = try { codec.dequeueOutputBuffer(info, 100_000) } catch (_: Exception) { break }
            if (i < 0) continue
            val buf = codec.getOutputBuffer(i)
            if (buf != null && info.size > 0) {
                buf.position(info.offset); buf.limit(info.offset + info.size)
                val config = info.flags and MediaCodec.BUFFER_FLAG_CODEC_CONFIG != 0
                val key = info.flags and MediaCodec.BUFFER_FLAG_KEY_FRAME != 0
                out.video(config, key, if (config) 0 else info.presentationTimeUs, buf, info.size)
            }
            try { codec.releaseOutputBuffer(i, false) } catch (_: Exception) { break }
        }
    }

    fun requestKeyframe() {
        try { codec.setParameters(Bundle().apply { putInt(MediaCodec.PARAMETER_KEY_REQUEST_SYNC_FRAME, 0) }) } catch (_: Exception) {}
    }

    fun release() {
        running = false
        try { drainer.join(500) } catch (_: Exception) {}
        try { codec.stop() } catch (_: Exception) {}
        try { codec.release() } catch (_: Exception) {}
        try { surface.release() } catch (_: Exception) {}
    }
}

private fun even16(v: Int) = maxOf(16, v / 16 * 16)

/** Screen casting: MediaProjection of the default display, rotation-aware. */
class ScreenStream(private val hello: JSONObject, private val out: StreamServer.Out) : StreamServer.Stream {
    private var projection: MediaProjection? = null
    private var vd: VirtualDisplay? = null
    private var enc: VideoEncoder? = null
    private val fps = hello.optInt("fps", 60).coerceIn(15, 120)
    private val bitrate = hello.optInt("bitrate", 8_000_000)
    private val maxSize = hello.optInt("max_size", 1920)
    private val hevc = hello.optString("codec") == "h265"
    private var size = Size(0, 0)
    private val dm = Hub.ctx.getSystemService(DisplayManager::class.java)
    private val ht = HandlerThread("cast").apply { start() }
    private val h = Handler(ht.looper)

    private fun target(): Size {
        val wm = Hub.ctx.createDisplayContext(dm.getDisplay(Display.DEFAULT_DISPLAY)).getSystemService(WindowManager::class.java)
        val b: Rect = if (Build.VERSION.SDK_INT >= 30) wm.maximumWindowMetrics.bounds else Rect(0, 0, 1080, 1920)
        val long = maxOf(b.width(), b.height())
        val k = if (maxSize in 1 until long) maxSize.toDouble() / long else 1.0
        return Size(even16((b.width() * k).toInt()), even16((b.height() * k).toInt()))
    }

    override fun start(): JSONObject {
        val p = Projection.acquire(60_000) ?: return JSONObject().put("ok", false).put("needs", Projection.lastNeeds)
            .put("error", Projection.lastError)
        projection = p
        size = target()
        enc = VideoEncoder(size.width, size.height, fps, bitrate, out, hevc)
        val dpi = Hub.ctx.resources.displayMetrics.densityDpi
        p.registerCallback(object : MediaProjection.Callback() {     // 14+: before createVirtualDisplay
            override fun onStop() { out.ended("casting stopped on the phone") }
        }, h)
        vd = p.createVirtualDisplay("Symphone cast", size.width, size.height, dpi,
            DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR, enc!!.surface, null, h)
        dm.registerDisplayListener(listener, h)
        return JSONObject().put("ok", true).put("width", size.width).put("height", size.height).put("codec", enc!!.name)
            .put("device", Build.MODEL).put("control", ControlService.instance != null)
    }

    private val listener = object : DisplayManager.DisplayListener {
        override fun onDisplayAdded(id: Int) {}
        override fun onDisplayRemoved(id: Int) {}
        override fun onDisplayChanged(id: Int) {
            if (id != Display.DEFAULT_DISPLAY) return
            val t = target()
            if (t == size || vd == null) return
            // rotated: new encoder at the new size (the PC resets its decoder on the size event + config)
            size = t
            out.event(JSONObject().put("type", "size").put("width", t.width).put("height", t.height))
            val old = enc
            val e = VideoEncoder(t.width, t.height, fps, bitrate, out, hevc)
            enc = e
            vd?.resize(t.width, t.height, Hub.ctx.resources.displayMetrics.densityDpi)
            vd?.surface = e.surface
            old?.release()
        }
    }

    override fun input(m: JSONObject) {
        when (m.optString("t")) {
            "reset" -> enc?.requestKeyframe()
            "launch" -> Hub.ctx.packageManager.getLaunchIntentForPackage(m.optString("pkg"))?.let {
                it.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_RESET_TASK_IF_NEEDED)
                Handler(Looper.getMainLooper()).post { try { Hub.ctx.startActivity(it) } catch (e: Exception) { Log.w("SymphoneStream", "launch: $e") } }
            }
            "torch" -> Torch.set(m.optBoolean("on"))
            "rotate", "zoom" -> {}
            else -> ControlService.input(m)
        }
    }

    override fun stop() {
        try { dm.unregisterDisplayListener(listener) } catch (_: Exception) {}
        try { vd?.release() } catch (_: Exception) {}
        enc?.release()
        Projection.release(projection)
        ht.quitSafely()
    }
}

/** The phone's own camera (works with the phone locked: no adb, no UI). */
class CameraStream(private val hello: JSONObject, private val out: StreamServer.Out) : StreamServer.Stream {
    private val cm = Hub.ctx.getSystemService(CameraManager::class.java)
    private val ht = HandlerThread("cam").apply { start() }
    private val h = Handler(ht.looper)
    private var dev: CameraDevice? = null
    private var session: CameraCaptureSession? = null
    private var req: CaptureRequest.Builder? = null
    private var enc: VideoEncoder? = null
    private var zoom = 1f
    private var zoomRange: Range<Float>? = null

    @SuppressLint("MissingPermission")
    override fun start(): JSONObject {
        if (Hub.ctx.checkSelfPermission(Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED)
            return JSONObject().put("ok", false).put("needs", "permission").put("error", "Allow Camera for Symphone on the phone")
        val id = hello.optString("camera").takeIf { it.isNotEmpty() && it in cm.cameraIdList } ?: cm.cameraIdList.firstOrNull {
            val f = cm.getCameraCharacteristics(it).get(CameraCharacteristics.LENS_FACING)
            f == (if (hello.optString("facing") == "front") CameraCharacteristics.LENS_FACING_FRONT else CameraCharacteristics.LENS_FACING_BACK)
        } ?: cm.cameraIdList.firstOrNull() ?: return JSONObject().put("ok", false).put("error", "no camera")
        if (!MediaFgs.ensure(MediaFgs.CAMERA)) return JSONObject().put("ok", false).put("needs", "permission")
            .put("error", "Android didn't let Symphone use the camera in the background (allow 'Display over other apps')")
        val ch = cm.getCameraCharacteristics(id)
        if (Build.VERSION.SDK_INT >= 30) zoomRange = ch.get(CameraCharacteristics.CONTROL_ZOOM_RATIO_RANGE)
        val map = ch.get(CameraCharacteristics.SCALER_STREAM_CONFIGURATION_MAP)!!
        val maxSize = hello.optInt("max_size", 1920)
        val sizes = map.getOutputSizes(MediaCodec::class.java).filter { maxOf(it.width, it.height) <= maxOf(640, maxSize) }
        val sz = sizes.filter { kotlin.math.abs(it.width.toDouble() / it.height - 16.0 / 9) < 0.02 }.maxByOrNull { it.width * it.height }
            ?: sizes.maxByOrNull { it.width * it.height } ?: Size(1280, 720)
        enc = VideoEncoder(sz.width, sz.height, hello.optInt("fps", 30).coerceIn(15, 60), hello.optInt("bitrate", 12_000_000), out)
        val opened = CountDownLatch(1)
        var err: String? = null
        cm.openCamera(id, object : CameraDevice.StateCallback() {
            override fun onOpened(d: CameraDevice) {
                dev = d
                @Suppress("DEPRECATION")
                d.createCaptureSession(listOf(enc!!.surface), object : CameraCaptureSession.StateCallback() {
                    override fun onConfigured(s: CameraCaptureSession) {
                        session = s
                        req = d.createCaptureRequest(CameraDevice.TEMPLATE_RECORD).apply {
                            addTarget(enc!!.surface)
                            set(CaptureRequest.CONTROL_AE_TARGET_FPS_RANGE, Range(30, 30))
                        }
                        repeat()
                        opened.countDown()
                    }
                    override fun onConfigureFailed(s: CameraCaptureSession) { err = "camera session failed"; opened.countDown() }
                }, h)
            }
            override fun onDisconnected(d: CameraDevice) { out.ended("camera disconnected"); d.close() }
            override fun onError(d: CameraDevice, e: Int) { err = "camera error $e"; opened.countDown(); out.ended("camera error $e"); d.close() }
        }, h)
        if (!opened.await(8, TimeUnit.SECONDS) || err != null) {
            stop(); return JSONObject().put("ok", false).put("error", err ?: "camera didn't open")
        }
        return JSONObject().put("ok", true).put("width", sz.width).put("height", sz.height).put("codec", "h264")
            .put("device", Build.MODEL)
    }

    private fun repeat() {
        try { session?.setRepeatingRequest(req!!.build(), null, h) } catch (e: Exception) { Log.w("SymphoneStream", "camera: $e") }
    }

    override fun input(m: JSONObject) {
        when (m.optString("t")) {
            "reset" -> enc?.requestKeyframe()
            "torch" -> { req?.set(CaptureRequest.FLASH_MODE, if (m.optBoolean("on")) CaptureRequest.FLASH_MODE_TORCH else CaptureRequest.FLASH_MODE_OFF); repeat() }
            "zoom" -> if (Build.VERSION.SDK_INT >= 30) zoomRange?.let { r ->
                zoom = (if (m.optBoolean("in")) zoom * 1.25f else zoom / 1.25f).coerceIn(r.lower, r.upper)
                req?.set(CaptureRequest.CONTROL_ZOOM_RATIO, zoom); repeat()
            }
        }
    }

    override fun stop() {
        try { session?.close() } catch (_: Exception) {}
        try { dev?.close() } catch (_: Exception) {}
        enc?.release()
        ht.quitSafely()
        MediaFgs.release(MediaFgs.CAMERA)
    }
}

/** Phone audio for the PC: what the phone plays (casting consent) or its microphone. */
class AudioStream(private val hello: JSONObject, private val out: StreamServer.Out) : StreamServer.Stream {
    private var rec: AudioRecord? = null
    private var projection: MediaProjection? = null
    @Volatile private var running = true
    private val mic = hello.optString("audio") == "mic"

    @SuppressLint("MissingPermission")
    override fun start(): JSONObject {
        if (Hub.ctx.checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED)
            return JSONObject().put("ok", false).put("needs", "permission").put("error", "Allow Microphone for Symphone on the phone")
        val fmt = AudioFormat.Builder().setEncoding(AudioFormat.ENCODING_PCM_16BIT).setSampleRate(48000)
            .setChannelMask(AudioFormat.CHANNEL_IN_STEREO).build()
        val bufSize = maxOf(AudioRecord.getMinBufferSize(48000, AudioFormat.CHANNEL_IN_STEREO, AudioFormat.ENCODING_PCM_16BIT), 3840 * 4)
        rec = if (mic) {
            if (!MediaFgs.ensure(MediaFgs.MIC)) return JSONObject().put("ok", false).put("needs", "permission")
                .put("error", "Android didn't let Symphone use the microphone in the background")
            AudioRecord.Builder().setAudioSource(MediaRecorder.AudioSource.VOICE_COMMUNICATION).setAudioFormat(fmt)
                .setBufferSizeInBytes(bufSize).build()
        } else {
            val p = Projection.acquire(60_000) ?: return JSONObject().put("ok", false).put("needs", Projection.lastNeeds)
                .put("error", Projection.lastError)
            projection = p
            val cfg = AudioPlaybackCaptureConfiguration.Builder(p)
                .addMatchingUsage(AudioAttributes.USAGE_MEDIA).addMatchingUsage(AudioAttributes.USAGE_GAME)
                .addMatchingUsage(AudioAttributes.USAGE_UNKNOWN).build()
            AudioRecord.Builder().setAudioFormat(fmt).setBufferSizeInBytes(bufSize).setAudioPlaybackCaptureConfig(cfg).build()
        }
        rec!!.startRecording()
        thread(name = "audio-cap", isDaemon = true) {
            val b = ByteArray(3840)                 // 20 ms
            val t0 = System.nanoTime()
            while (running && !out.closed) {
                val n = rec?.read(b, 0, b.size) ?: break
                if (n <= 0) { if (n < 0) break else continue }
                out.audio((System.nanoTime() - t0) / 1000, b, n)
            }
        }
        return JSONObject().put("ok", true).put("width", 0).put("height", 0).put("codec", "pcm")
    }

    override fun input(m: JSONObject) {}

    override fun stop() {
        running = false
        try { rec?.stop() } catch (_: Exception) {}
        try { rec?.release() } catch (_: Exception) {}
        if (mic) MediaFgs.release(MediaFgs.MIC) else Projection.release(projection)
    }
}

/**
 * Screen-casting consent and the MediaProjection it yields. Android asks for every session (14+) unless Symphone has
 * the PROJECT_MEDIA app-op (set by the PC whenever it has adb); otherwise the Symphone control accessibility service
 * confirms the dialog. The consent screen is started by [ConsentActivity] (background starts need "Display over
 * other apps").
 */
object Projection {
    @Volatile var lastNeeds = "consent"
    @Volatile var lastError = "Screen casting wasn't allowed on the phone"
    private var waiting: CountDownLatch? = null
    @Volatile private var result: MediaProjection? = null
    private var users = 0
    @Volatile private var current: MediaProjection? = null

    /** Shared by a screen and an audio stream running at the same time; otherwise asks for consent. */
    @Synchronized fun acquire(timeoutMs: Long): MediaProjection? {
        current?.let { users++; return it }
        val noBackgroundStart = !Settings.canDrawOverlays(Hub.ctx) && !App.foreground
        val l = CountDownLatch(1); waiting = l; result = null
        ControlService.expectConsent()          // confirms the dialog if Android shows one (no PROJECT_MEDIA app-op)
        Handler(Looper.getMainLooper()).post {
            try {
                Hub.ctx.startActivity(Intent(Hub.ctx, ConsentActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK
                    or Intent.FLAG_ACTIVITY_NO_ANIMATION or Intent.FLAG_ACTIVITY_EXCLUDE_FROM_RECENTS))
            } catch (e: Exception) { Log.w("SymphoneStream", "consent: $e"); l.countDown() }
        }
        l.await(timeoutMs, TimeUnit.MILLISECONDS)
        val p = result ?: run {
            if (noBackgroundStart) { lastNeeds = "permission"; lastError = "Allow 'Display over other apps' for Symphone so it can ask to cast the screen" }
            else { lastNeeds = "consent"; lastError = "Screen casting wasn't allowed on the phone" }
            return null
        }
        current = p; users = 1
        return p
    }

    fun deliver(p: MediaProjection?) { result = p; waiting?.countDown() }

    @Synchronized fun release(p: MediaProjection?) {
        if (p == null || p !== current) return
        if (--users > 0) return
        try { p.stop() } catch (_: Exception) {}
        current = null
        MediaFgs.release(MediaFgs.PROJECTION)
    }
}

/** Transparent activity: asks for the screen-casting consent, then hands it to [MediaFgs]. */
class ConsentActivity : Activity() {
    override fun onCreate(b: Bundle?) {
        super.onCreate(b)
        val mpm = getSystemService(MediaProjectionManager::class.java)
        // 14+: ask for the whole screen directly, so the dialog has no "a single app" choice to make
        val intent = if (Build.VERSION.SDK_INT >= 34)
            mpm.createScreenCaptureIntent(android.media.projection.MediaProjectionConfig.createConfigForDefaultDisplay())
        else mpm.createScreenCaptureIntent()
        try { startActivityForResult(intent, 1) } catch (e: Exception) { Projection.deliver(null); finish() }
    }

    @Deprecated("Deprecated in Java")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        if (resultCode == RESULT_OK && data != null) {
            MediaFgs.projectionResult = resultCode to data
            startForegroundService(Intent(this, MediaFgs::class.java).setAction(MediaFgs.ACTION_PROJECTION))
        } else Projection.deliver(null)
        finish()
        overridePendingTransition(0, 0)
    }
}

/** Transparent activity: being in the foreground for a moment lets Android start the camera/mic service. */
class StreamTrampoline : Activity() {
    override fun onCreate(b: Bundle?) {
        super.onCreate(b)
        try { startForegroundService(Intent(this, MediaFgs::class.java).setAction(MediaFgs.ACTION_UPDATE)) }
        catch (e: Exception) { Log.w("SymphoneStream", "trampoline: $e") }
        finish()
        overridePendingTransition(0, 0)
    }
}

/** Foreground service for screen casting / camera / microphone (types change as streams come and go). */
class MediaFgs : Service() {
    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(NotificationChannel("media", "Streaming to the PC", NotificationManager.IMPORTANCE_LOW))
        val n = Notification.Builder(this, "media").setSmallIcon(R.drawable.ic_stat)
            .setContentTitle("Streaming to your PC").setOngoing(true).build()
        try {
            if (intent?.action == ACTION_PROJECTION) active = active or PROJECTION
            val t = types()
            if (t == 0) { stopSelf(); return START_NOT_STICKY }
            startForeground(7, n, t)
            if (intent?.action == ACTION_PROJECTION) {
                val (code, data) = projectionResult ?: (0 to null)
                projectionResult = null
                val p = if (data != null) getSystemService(MediaProjectionManager::class.java).getMediaProjection(code, data) else null
                Projection.deliver(p)
            }
            started?.countDown()
        } catch (e: Exception) {
            Log.w("SymphoneStream", "fgs: $e")
            if (intent?.action == ACTION_PROJECTION) Projection.deliver(null)
            failed = true; started?.countDown()
        }
        return START_NOT_STICKY
    }

    private fun types(): Int {
        var t = 0
        if (active and PROJECTION != 0) t = t or ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION
        if (active and CAMERA != 0) t = t or ServiceInfo.FOREGROUND_SERVICE_TYPE_CAMERA
        if (active and MIC != 0) t = t or ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE
        return t
    }

    companion object {
        const val ACTION_PROJECTION = "projection"
        const val ACTION_UPDATE = "update"
        const val PROJECTION = 1; const val CAMERA = 2; const val MIC = 4
        @Volatile var active = 0
        @Volatile var projectionResult: Pair<Int, Intent>? = null
        @Volatile private var started: CountDownLatch? = null
        @Volatile private var failed = false

        /** Adds camera/mic to the service; from the background via a brief transparent activity. */
        @Synchronized fun ensure(what: Int): Boolean {
            if (active and what != 0) return true
            active = active or what
            val l = CountDownLatch(1); started = l; failed = false
            val ctx = Hub.ctx
            Handler(Looper.getMainLooper()).post {
                try {
                    if (App.foreground) ctx.startForegroundService(Intent(ctx, MediaFgs::class.java).setAction(ACTION_UPDATE))
                    else ctx.startActivity(Intent(ctx, StreamTrampoline::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK
                        or Intent.FLAG_ACTIVITY_NO_ANIMATION or Intent.FLAG_ACTIVITY_EXCLUDE_FROM_RECENTS))
                } catch (e: Exception) {
                    Log.w("SymphoneStream", "ensure: $e")
                    try { ctx.startForegroundService(Intent(ctx, MediaFgs::class.java).setAction(ACTION_UPDATE)) }
                    catch (e2: Exception) { failed = true; l.countDown() }
                }
            }
            val ok = l.await(5, TimeUnit.SECONDS) && !failed
            if (!ok) active = active and what.inv()
            return ok
        }

        fun release(what: Int) {
            active = active and what.inv()
            val ctx = Hub.ctx
            try {
                if (active == 0) ctx.stopService(Intent(ctx, MediaFgs::class.java))
                else ctx.startForegroundService(Intent(ctx, MediaFgs::class.java).setAction(ACTION_UPDATE))
            } catch (_: Exception) {}
        }
    }
}

package dev.phonesync

import android.app.Activity
import android.content.Intent
import android.content.pm.ActivityInfo
import android.content.res.Configuration
import android.graphics.Color
import android.media.MediaCodec
import android.media.MediaCodecInfo
import android.media.MediaFormat
import android.os.Bundle
import android.os.Handler
import android.os.HandlerThread
import android.util.Log
import android.view.Gravity
import android.view.MotionEvent
import android.view.Surface
import android.view.SurfaceHolder
import android.view.SurfaceView
import android.view.View
import android.view.WindowInsets
import android.view.WindowInsetsController
import android.view.WindowManager
import android.widget.FrameLayout
import android.widget.TextView
import org.json.JSONObject
import java.nio.ByteBuffer
import java.util.concurrent.LinkedBlockingDeque
import java.util.concurrent.TimeUnit

/**
 * Sidecar: the phone shows a Windows virtual monitor (H.264 from the PC) and sends touches back.
 *
 * Started with `adb shell am start -n dev.phonesync/.SidecarActivity [--es orientation landscape|portrait]`.
 * Listens on [PORT] while open (handshake: see [SidecarLink]).
 * PC -> phone: type 3 json {"video":{"w":..,"h":..}} | {"cmd":"close"} | {"orientation":"landscape|portrait"}
 *              type 4 video: u8 flags(bit0 key) u64 ptsUs + Annex-B access unit
 * phone -> PC: type 1 touch frame (u32 time, u8 action, u8 count, then per pointer: u8 id, u8 flags, u16 x, u16 y, u16 major, u16 pressure, u16 1000; x/y normalized to the video area)
 *              type 3 json {"hello":1,"w":..,"h":..,"xdpi":..,"ydpi":..} | {"need_key":1} | {"decoder":"name"}
 *              type 6 rendered: u64 ptsUs of each frame the moment it was rendered (latency measurement)
 */
class SidecarActivity : Activity(), SurfaceHolder.Callback {
    companion object { const val PORT = 47903; const val TAG = "Symphone" }

    private lateinit var surfaceView: SurfaceView
    private lateinit var statusText: TextView
    private lateinit var link: SidecarLink
    @Volatile private var surface: Surface? = null

    private val frames = LinkedBlockingDeque<ByteArray>(12)
    @Volatile private var videoW = 0
    @Volatile private var videoH = 0
    @Volatile private var running = true
    private var decodeThread: Thread? = null
    private val cbThread = HandlerThread("sidecar-cb").apply { start() }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        setShowWhenLocked(true)
        setTurnScreenOn(true)
        applyOrientation(intent?.getStringExtra("orientation"))
        surfaceView = SurfaceView(this)
        surfaceView.holder.addCallback(this)
        statusText = TextView(this).apply {
            setTextColor(Color.rgb(140, 145, 155)); textSize = 15f; gravity = Gravity.CENTER
            text = "Sidecar · waiting for PC…"
        }
        val root = FrameLayout(this).apply {
            setBackgroundColor(Color.BLACK)
            addView(surfaceView, FrameLayout.LayoutParams(-1, -1))
            addView(statusText, FrameLayout.LayoutParams(-1, -1))
        }
        setContentView(root)
        surfaceView.setOnTouchListener { v, e -> onTouch(v, e) }
        immersive()
        link = SidecarLink(PORT, "sidecar", ::onFrame) { c ->
            runOnUiThread {
                if (c) { sendHello(); link.sendJson(JSONObject().put("need_key", 1)) }
                else { statusText.visibility = View.VISIBLE; statusText.text = "Sidecar · waiting for PC…" }
            }
        }
        link.start()
        decodeThread = Thread({ decodeLoop() }, "sidecar-decode").apply { priority = Thread.MAX_PRIORITY; start() }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        applyOrientation(intent.getStringExtra("orientation"))
    }

    private fun applyOrientation(o: String?) {
        requestedOrientation = when (o) {
            "portrait" -> ActivityInfo.SCREEN_ORIENTATION_SENSOR_PORTRAIT
            "sensor" -> ActivityInfo.SCREEN_ORIENTATION_FULL_USER
            else -> ActivityInfo.SCREEN_ORIENTATION_SENSOR_LANDSCAPE
        }
    }

    override fun onWindowFocusChanged(hasFocus: Boolean) {
        super.onWindowFocusChanged(hasFocus)
        if (hasFocus) immersive()
    }

    override fun onConfigurationChanged(newConfig: Configuration) {
        super.onConfigurationChanged(newConfig)
        immersive()
        surfaceView.post { sendHello() }
    }

    private fun immersive() {
        window.insetsController?.let {
            it.hide(WindowInsets.Type.systemBars())
            it.systemBarsBehavior = WindowInsetsController.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE
        }
        window.attributes = window.attributes.apply {
            layoutInDisplayCutoutMode = WindowManager.LayoutParams.LAYOUT_IN_DISPLAY_CUTOUT_MODE_ALWAYS
        }
    }

    override fun onDestroy() {
        running = false
        link.stop()
        decodeThread?.interrupt()
        cbThread.quitSafely()
        super.onDestroy()
    }

    private fun sendHello() {
        if (!link.connected || surfaceView.width == 0) return
        val dm = resources.displayMetrics
        link.sendJson(JSONObject().put("hello", 1).put("w", surfaceView.width).put("h", surfaceView.height)
            .put("xdpi", dm.xdpi.toDouble()).put("ydpi", dm.ydpi.toDouble()).put("rotation", display?.rotation ?: 0))
    }

    // ---------------------------------------------------------------- surface
    override fun surfaceCreated(holder: SurfaceHolder) { surface = holder.surface; requestRestart() }
    override fun surfaceChanged(holder: SurfaceHolder, format: Int, width: Int, height: Int) {
        surfaceView.systemGestureExclusionRects = listOf(android.graphics.Rect(0, 0, width, height))
        sendHello()
    }
    override fun surfaceDestroyed(holder: SurfaceHolder) { surface = null; requestRestart() }

    @Volatile private var restart = false
    private fun requestRestart() { restart = true; frames.offerFirst(ByteArray(0)) }

    // ---------------------------------------------------------------- network
    private fun onFrame(type: Int, buf: ByteArray, len: Int) {
        when (type) {
            4 -> {
                val f = buf.copyOf(len)
                if (!frames.offer(f)) {
                    // Decoder can't keep up: drop everything queued and resync on the next keyframe.
                    frames.clear(); frames.offer(f)
                    link.sendJson(JSONObject().put("need_key", 1))
                }
            }
            3 -> {
                val o = JSONObject(String(buf, 0, len))
                o.optJSONObject("video")?.let {
                    val w = it.getInt("w"); val h = it.getInt("h")
                    if (w != videoW || h != videoH) { videoW = w; videoH = h; requestRestart() }
                }
                o.optString("orientation").takeIf { it.isNotEmpty() }?.let { runOnUiThread { applyOrientation(it) } }
                if (o.optString("cmd") == "close") runOnUiThread { finish() }
            }
        }
    }

    // ---------------------------------------------------------------- decoding
    private fun decodeLoop() {
        var codec: MediaCodec? = null
        var waitingKey = true
        val info = MediaCodec.BufferInfo()
        while (running) {
            try {
                val f = frames.poll(20, TimeUnit.MILLISECONDS)
                if (restart) {
                    restart = false
                    codec?.let { try { it.stop(); it.release() } catch (_: Exception) {} }
                    codec = null
                    waitingKey = true
                }
                if (f != null && f.size > 9) {
                    val key = (f[0].toInt() and 1) != 0
                    val pts = ByteBuffer.wrap(f, 1, 8).long
                    if (codec == null) {
                        val s = surface
                        if (key && s != null && s.isValid && videoW > 0) {
                            codec = createDecoder(f, s)
                            waitingKey = codec == null
                        } else if (key && videoW == 0) {
                            link.sendJson(JSONObject().put("need_key", 1))
                        }
                    }
                    val c = codec
                    if (c != null && !(waitingKey && !key)) {
                        waitingKey = false
                        val idx = c.dequeueInputBuffer(30_000)
                        if (idx >= 0) {
                            val ib = c.getInputBuffer(idx)!!
                            ib.clear()
                            val n = minOf(f.size - 9, ib.remaining())
                            ib.put(f, 9, n)
                            c.queueInputBuffer(idx, 0, n, pts, 0)
                        } else {
                            waitingKey = true
                            link.sendJson(JSONObject().put("need_key", 1))
                        }
                    }
                }
                // Render everything that is ready, immediately.
                val c = codec ?: continue
                while (true) {
                    val o = c.dequeueOutputBuffer(info, 0)
                    if (o >= 0) c.releaseOutputBuffer(o, true)
                    else if (o == MediaCodec.INFO_TRY_AGAIN_LATER) break
                }
            } catch (_: InterruptedException) {
                break
            } catch (e: Exception) {
                Log.w(TAG, "sidecar decode: $e")
                codec?.let { try { it.release() } catch (_: Exception) {} }
                codec = null
                waitingKey = true
                link.sendJson(JSONObject().put("need_key", 1))
            }
        }
        codec?.let { try { it.stop(); it.release() } catch (_: Exception) {} }
    }

    private fun createDecoder(key: ByteArray, s: Surface): MediaCodec? {
        val fmt = MediaFormat.createVideoFormat(MediaFormat.MIMETYPE_VIDEO_AVC, videoW, videoH)
        // SPS/PPS from the first keyframe as codec-specific data.
        val nals = splitNals(key, 9)
        nals.firstOrNull { (key[it.first].toInt() and 0x1f) == 7 }?.let { fmt.setByteBuffer("csd-0", annexB(key, it)) }
        nals.firstOrNull { (key[it.first].toInt() and 0x1f) == 8 }?.let { fmt.setByteBuffer("csd-1", annexB(key, it)) }
        fmt.setInteger(MediaFormat.KEY_MAX_INPUT_SIZE, videoW * videoH)
        fmt.setInteger(MediaFormat.KEY_LOW_LATENCY, 1)
        fmt.setInteger(MediaFormat.KEY_PRIORITY, 0)
        fmt.setInteger(MediaFormat.KEY_OPERATING_RATE, 120)
        fmt.setInteger("vendor.qti-ext-dec-low-latency.enable", 1)
        fmt.setInteger("vendor.qti-ext-dec-picture-order.enable", 1)
        val c = try { MediaCodec.createDecoderByType(MediaFormat.MIMETYPE_VIDEO_AVC) } catch (e: Exception) { return null }
        return try {
            c.configure(fmt, s, null, 0)
            c.setVideoScalingMode(MediaCodec.VIDEO_SCALING_MODE_SCALE_TO_FIT)
            c.setOnFrameRenderedListener({ _, pts, _ ->
                val b = ByteBuffer.allocate(8).putLong(pts).array()
                link.send(6, b)
            }, Handler(cbThread.looper))
            c.start()
            val ll = try { c.codecInfo.getCapabilitiesForType(MediaFormat.MIMETYPE_VIDEO_AVC)
                .isFeatureSupported(MediaCodecInfo.CodecCapabilities.FEATURE_LowLatency) } catch (_: Exception) { false }
            link.sendJson(JSONObject().put("decoder", c.name).put("low_latency", ll).put("w", videoW).put("h", videoH))
            runOnUiThread { statusText.visibility = View.GONE }
            c
        } catch (e: Exception) {
            Log.w(TAG, "sidecar configure: $e")
            try { c.release() } catch (_: Exception) {}
            null
        }
    }

    /** (start, end) offsets of NAL payloads (after the start code) in an Annex-B buffer. */
    private fun splitNals(b: ByteArray, from: Int): List<Pair<Int, Int>> {
        val starts = ArrayList<Int>()
        var i = from
        while (i + 3 < b.size) {
            if (b[i].toInt() == 0 && b[i + 1].toInt() == 0 && (b[i + 2].toInt() == 1 || (b[i + 2].toInt() == 0 && b[i + 3].toInt() == 1))) {
                val sc = if (b[i + 2].toInt() == 1) 3 else 4
                starts.add(i + sc); i += sc
            } else i++
        }
        return starts.mapIndexed { k, s ->
            var e = if (k + 1 < starts.size) starts[k + 1] else b.size
            // back off over the next start code (and any trailing zero byte)
            if (k + 1 < starts.size) { e -= 3; while (e > s && b[e - 1].toInt() == 0) e-- }
            s to e
        }
    }

    private fun annexB(b: ByteArray, r: Pair<Int, Int>): ByteBuffer {
        val out = ByteBuffer.allocate(4 + r.second - r.first)
        out.put(byteArrayOf(0, 0, 0, 1)); out.put(b, r.first, r.second - r.first); out.flip()
        return out
    }

    // ---------------------------------------------------------------- touch -> PC
    private val tbuf = ByteBuffer.allocate(6 + 10 * 13)

    private fun onTouch(v: View, e: MotionEvent): Boolean {
        val w = v.width.coerceAtLeast(1).toFloat(); val h = v.height.coerceAtLeast(1).toFloat()
        val am = e.actionMasked; val ai = e.actionIndex
        tbuf.clear()
        tbuf.putInt(e.eventTime.toInt()); tbuf.put(am.toByte()); tbuf.put(e.pointerCount.toByte())
        for (i in 0 until e.pointerCount) {
            val lifting = am == MotionEvent.ACTION_UP || am == MotionEvent.ACTION_CANCEL ||
                    (am == MotionEvent.ACTION_POINTER_UP && i == ai)
            var flags = 0
            if (lifting) flags = flags or 1
            if (am == MotionEvent.ACTION_CANCEL) flags = flags or 4
            if (e.getToolType(i) == MotionEvent.TOOL_TYPE_STYLUS) flags = flags or 8
            tbuf.put(e.getPointerId(i).toByte()); tbuf.put(flags.toByte())
            tbuf.putShort((e.getX(i) / w * 65535f).toInt().coerceIn(0, 65535).toShort())
            tbuf.putShort((e.getY(i) / h * 65535f).toInt().coerceIn(0, 65535).toShort())
            tbuf.putShort(e.getTouchMajor(i).toInt().coerceIn(0, 65535).toShort())
            tbuf.putShort((e.getPressure(i) * 10000f).toInt().coerceIn(0, 65535).toShort())
            tbuf.putShort(1000)
        }
        link.send(1, tbuf.array().copyOf(tbuf.position()), droppable = am == MotionEvent.ACTION_MOVE)
        return true
    }
}

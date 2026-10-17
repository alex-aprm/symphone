package dev.phonesync

import android.Manifest
import android.annotation.SuppressLint
import android.app.Activity
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.graphics.ImageFormat
import android.graphics.Matrix
import android.graphics.SurfaceTexture
import android.graphics.drawable.GradientDrawable
import android.hardware.camera2.CameraCaptureSession
import android.hardware.camera2.CameraCharacteristics
import android.hardware.camera2.CameraDevice
import android.hardware.camera2.CameraManager
import android.hardware.camera2.CaptureRequest
import android.hardware.camera2.params.OutputConfiguration
import android.hardware.camera2.params.SessionConfiguration
import android.media.ImageReader
import android.net.Uri
import android.os.Bundle
import android.os.Handler
import android.os.HandlerThread
import android.os.VibrationEffect
import android.os.VibratorManager
import android.util.Log
import android.util.Size
import android.view.Gravity
import android.view.HapticFeedbackConstants
import android.view.Surface
import android.view.TextureView
import android.widget.FrameLayout
import android.widget.TextView
import com.google.zxing.BarcodeFormat
import com.google.zxing.BinaryBitmap
import com.google.zxing.DecodeHintType
import com.google.zxing.NotFoundException
import com.google.zxing.PlanarYUVLuminanceSource
import com.google.zxing.common.HybridBinarizer
import com.google.zxing.qrcode.QRCodeReader
import java.util.concurrent.Executor
import java.util.concurrent.atomic.AtomicBoolean
import kotlin.math.abs
import kotlin.math.max

/**
 * In-app QR scanner: Camera2 preview on a TextureView, ~720p YUV_420_888 frames from an ImageReader decoded with
 * ZXing core (QR only). A Symphone pairing link starts [PairActivity]; a haptic tick confirms the scan.
 */
class ScanActivity : Activity() {
    private val tag = "SymphoneScan"
    private lateinit var ui: Ui
    private lateinit var texture: TextureView
    private lateinit var hint: TextView
    private var camThread: HandlerThread? = null
    private var camHandler: Handler? = null
    private var device: CameraDevice? = null
    private var session: CameraCaptureSession? = null
    private var reader: ImageReader? = null
    private var bufSize = Size(1280, 720)
    private var sensorOrientation = 90
    private val decoding = AtomicBoolean(false)
    private val done = AtomicBoolean(false)
    private val qr = QRCodeReader()
    private val hints = mapOf(
        DecodeHintType.POSSIBLE_FORMATS to listOf(BarcodeFormat.QR_CODE),
        DecodeHintType.CHARACTER_SET to "UTF-8",
    )
    private var lastWrongToast = 0L
    private var lum: ByteArray? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        ui = Ui(this)
        texture = TextureView(this)
        val frame = android.view.View(this).apply {
            background = GradientDrawable().apply {
                setColor(Color.TRANSPARENT); cornerRadius = ui.dp(28f); setStroke(ui.dp(3), Color.WHITE)
            }
        }
        hint = TextView(this).apply {
            text = "Point the camera at the QR code shown on your PC\n(Symphone → Settings → Add phone)"
            setTextColor(Color.WHITE); textSize = 15f; gravity = Gravity.CENTER
            setShadowLayer(8f, 0f, 0f, Color.BLACK)
        }
        val close = TextView(this).apply {
            text = "✕"; textSize = 22f; setTextColor(Color.WHITE); gravity = Gravity.CENTER
            background = GradientDrawable().apply { shape = GradientDrawable.OVAL; setColor(0x66000000) }
            setOnClickListener { finish() }
        }
        val root = FrameLayout(this).apply {
            setBackgroundColor(Color.BLACK)
            addView(texture, FrameLayout.LayoutParams(-1, -1))
            val side = (resources.displayMetrics.widthPixels * 0.68f).toInt()
            addView(frame, FrameLayout.LayoutParams(side, side, Gravity.CENTER))
            addView(hint, FrameLayout.LayoutParams(-1, -2, Gravity.BOTTOM).apply { bottomMargin = ui.dp(96) })
            addView(close, FrameLayout.LayoutParams(ui.dp(48), ui.dp(48), Gravity.TOP or Gravity.START).apply {
                topMargin = ui.dp(48); leftMargin = ui.dp(16)
            })
        }
        setContentView(root)
        window.statusBarColor = Color.TRANSPARENT
        window.navigationBarColor = Color.TRANSPARENT
        window.setDecorFitsSystemWindows(false)
    }

    override fun onResume() {
        super.onResume()
        if (checkSelfPermission(Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(arrayOf(Manifest.permission.CAMERA), 7)
            return
        }
        startCamera()
    }

    override fun onPause() {
        stopCamera()
        super.onPause()
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        if (grantResults.firstOrNull() == PackageManager.PERMISSION_GRANTED) startCamera()
        else { hint.text = "Camera permission is needed to scan the pairing code."; }
    }

    private fun startCamera() {
        if (device != null || done.get()) return
        if (texture.isAvailable) openCamera()
        else texture.surfaceTextureListener = object : TextureView.SurfaceTextureListener {
            override fun onSurfaceTextureAvailable(st: SurfaceTexture, w: Int, h: Int) = openCamera()
            override fun onSurfaceTextureSizeChanged(st: SurfaceTexture, w: Int, h: Int) = applyTransform()
            override fun onSurfaceTextureDestroyed(st: SurfaceTexture) = true
            override fun onSurfaceTextureUpdated(st: SurfaceTexture) {}
        }
    }

    @SuppressLint("MissingPermission")
    private fun openCamera() {
        if (device != null) return
        val cm = getSystemService(Context.CAMERA_SERVICE) as CameraManager
        val id = try {
            cm.cameraIdList.firstOrNull { cm.getCameraCharacteristics(it).get(CameraCharacteristics.LENS_FACING) == CameraCharacteristics.LENS_FACING_BACK }
                ?: cm.cameraIdList.firstOrNull()
        } catch (e: Exception) { null }
        if (id == null) { hint.text = "No camera available."; return }
        val ch = cm.getCameraCharacteristics(id)
        sensorOrientation = ch.get(CameraCharacteristics.SENSOR_ORIENTATION) ?: 90
        val map = ch.get(CameraCharacteristics.SCALER_STREAM_CONFIGURATION_MAP)
        val sizes = map?.getOutputSizes(ImageFormat.YUV_420_888)?.toList().orEmpty()
        // ~720p, 16:9 preferred.
        bufSize = sizes.minByOrNull { abs(it.width * it.height - 1280 * 720) + (if (it.width * 9 == it.height * 16) 0 else 200_000) } ?: Size(1280, 720)
        val t = HandlerThread("scan-cam").apply { start() }
        camThread = t
        camHandler = Handler(t.looper)
        val r = ImageReader.newInstance(bufSize.width, bufSize.height, ImageFormat.YUV_420_888, 3)
        r.setOnImageAvailableListener({ onImage(it) }, camHandler)
        reader = r
        try {
            cm.openCamera(id, object : CameraDevice.StateCallback() {
                override fun onOpened(d: CameraDevice) { device = d; createSession(d) }
                override fun onDisconnected(d: CameraDevice) { d.close(); if (device === d) device = null }
                override fun onError(d: CameraDevice, error: Int) {
                    Log.w(tag, "camera error $error"); d.close(); if (device === d) device = null
                    runOnUiThread { hint.text = "Camera unavailable ($error)." }
                }
            }, camHandler)
        } catch (e: Exception) {
            Log.w(tag, "open: $e"); hint.text = "Camera unavailable."
        }
    }

    private fun createSession(d: CameraDevice) {
        val st = texture.surfaceTexture ?: return
        st.setDefaultBufferSize(bufSize.width, bufSize.height)
        runOnUiThread { applyTransform() }
        val preview = Surface(st)
        val rs = reader!!.surface
        val exec = Executor { camHandler?.post(it) }
        val cfg = SessionConfiguration(SessionConfiguration.SESSION_REGULAR,
            listOf(OutputConfiguration(preview), OutputConfiguration(rs)), exec,
            object : CameraCaptureSession.StateCallback() {
                override fun onConfigured(s: CameraCaptureSession) {
                    session = s
                    try {
                        val req = d.createCaptureRequest(CameraDevice.TEMPLATE_PREVIEW).apply {
                            addTarget(preview); addTarget(rs)
                            set(CaptureRequest.CONTROL_AF_MODE, CaptureRequest.CONTROL_AF_MODE_CONTINUOUS_PICTURE)
                            set(CaptureRequest.CONTROL_MODE, CaptureRequest.CONTROL_MODE_AUTO)
                        }
                        s.setRepeatingRequest(req.build(), null, camHandler)
                    } catch (e: Exception) { Log.w(tag, "repeat: $e") }
                }
                override fun onConfigureFailed(s: CameraCaptureSession) { Log.w(tag, "configure failed") }
            })
        try { d.createCaptureSession(cfg) } catch (e: Exception) { Log.w(tag, "session: $e") }
    }

    /** Center-crop the (sensor-rotated) preview into the view. The activity is portrait-only. */
    private fun applyTransform() {
        val vw = texture.width.toFloat(); val vh = texture.height.toFloat()
        if (vw == 0f || vh == 0f) return
        val rotated = sensorOrientation % 180 != 0
        val cw = (if (rotated) bufSize.height else bufSize.width).toFloat()
        val ch = (if (rotated) bufSize.width else bufSize.height).toFloat()
        val scale = max(vw / cw, vh / ch)
        val m = Matrix()
        m.setScale(cw * scale / vw, ch * scale / vh, vw / 2, vh / 2)
        texture.setTransform(m)
    }

    private fun onImage(r: ImageReader) {
        val img = try { r.acquireLatestImage() } catch (_: Exception) { null } ?: return
        try {
            if (done.get() || !decoding.compareAndSet(false, true)) return
            val plane = img.planes[0]
            val w = img.width; val h = img.height
            val stride = plane.rowStride
            val buf = plane.buffer
            val need = stride * h
            val data = lum?.takeIf { it.size == need } ?: ByteArray(need).also { lum = it }
            buf.rewind()
            buf.get(data, 0, minOf(buf.remaining(), need))
            try {
                val src = PlanarYUVLuminanceSource(data, stride, h, 0, 0, w, h, false)
                val res = qr.decode(BinaryBitmap(HybridBinarizer(src)), hints)
                onDecoded(res.text)
            } catch (_: NotFoundException) {
            } catch (_: com.google.zxing.ChecksumException) {
            } catch (_: com.google.zxing.FormatException) {
            } finally { qr.reset() }
        } finally {
            img.close()
            decoding.set(false)
        }
    }

    private fun onDecoded(text: String) {
        val req = Pairing.parse(text)
        if (req == null) {
            val now = System.currentTimeMillis()
            if (now - lastWrongToast > 3000) {
                lastWrongToast = now
                runOnUiThread { hint.text = "That's not a Symphone pairing code.\nOpen Symphone on your PC → Settings → Add phone." }
            }
            return
        }
        if (!done.compareAndSet(false, true)) return
        tick()
        runOnUiThread {
            val uri = Uri.parse(text.trim())
            PairActivity.trustedUri = uri
            startActivity(Intent(Intent.ACTION_VIEW, uri, this, PairActivity::class.java))
            finish()
        }
    }

    private fun tick() {
        try {
            val v = if (android.os.Build.VERSION.SDK_INT >= 31) getSystemService(VibratorManager::class.java)?.defaultVibrator
                else @Suppress("DEPRECATION") getSystemService(android.os.Vibrator::class.java)
            v?.vibrate(VibrationEffect.createPredefined(VibrationEffect.EFFECT_TICK))
        } catch (_: Exception) {}
        texture.post { texture.performHapticFeedback(HapticFeedbackConstants.CONFIRM) }
    }

    private fun stopCamera() {
        try { session?.close() } catch (_: Exception) {}
        session = null
        try { device?.close() } catch (_: Exception) {}
        device = null
        try { reader?.close() } catch (_: Exception) {}
        reader = null
        camThread?.quitSafely()
        camThread = null
        camHandler = null
    }
}

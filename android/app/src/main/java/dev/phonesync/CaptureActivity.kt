package dev.phonesync

import android.app.Activity
import android.content.ContentValues
import android.content.Context
import android.graphics.ImageFormat
import android.graphics.SurfaceTexture
import android.hardware.camera2.CameraCaptureSession
import android.hardware.camera2.CameraCharacteristics
import android.hardware.camera2.CameraDevice
import android.hardware.camera2.CameraManager
import android.hardware.camera2.CaptureRequest
import android.hardware.camera2.params.OutputConfiguration
import android.hardware.camera2.params.SessionConfiguration
import android.media.ImageReader
import android.os.Bundle
import android.os.Handler
import android.os.HandlerThread
import android.provider.MediaStore
import android.util.Size
import android.view.Surface
import android.view.TextureView
import android.view.WindowManager
import org.json.JSONObject
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.concurrent.Executor

/**
 * One-shot still capture started from the PC. Shows a brief preview (AE/AF settle), shoots the
 * largest JPEG, saves it to DCIM/Symphone and closes. Works over the lock screen like a camera app.
 */
class CaptureActivity : Activity() {
    private val thread = HandlerThread("capture").apply { start() }
    private val bg = Handler(thread.looper)
    private val exec = Executor { bg.post(it) }
    private var device: CameraDevice? = null
    private var reader: ImageReader? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        val tv = TextureView(this)
        setContentView(tv)
        tv.surfaceTextureListener = object : TextureView.SurfaceTextureListener {
            override fun onSurfaceTextureAvailable(st: SurfaceTexture, w: Int, h: Int) = open(st)
            override fun onSurfaceTextureSizeChanged(st: SurfaceTexture, w: Int, h: Int) {}
            override fun onSurfaceTextureDestroyed(st: SurfaceTexture) = true
            override fun onSurfaceTextureUpdated(st: SurfaceTexture) {}
        }
        bg.postDelayed({ fail("timeout") }, 10_000)
    }

    @Suppress("MissingPermission")
    private fun open(st: SurfaceTexture) {
        val cm = getSystemService(Context.CAMERA_SERVICE) as CameraManager
        val wantFront = intent.getStringExtra("facing") == "front"
        val id = cm.cameraIdList.firstOrNull {
            val f = cm.getCameraCharacteristics(it).get(CameraCharacteristics.LENS_FACING)
            f == if (wantFront) CameraCharacteristics.LENS_FACING_FRONT else CameraCharacteristics.LENS_FACING_BACK
        } ?: cm.cameraIdList.firstOrNull() ?: return fail("no camera")
        val ch = cm.getCameraCharacteristics(id)
        val map = ch.get(CameraCharacteristics.SCALER_STREAM_CONFIGURATION_MAP)!!
        val jpeg = map.getOutputSizes(ImageFormat.JPEG).maxByOrNull { it.width.toLong() * it.height }!!
        val preview = map.getOutputSizes(SurfaceTexture::class.java)
            .filter { it.width <= 1920 && it.height <= 1080 }
            .minByOrNull { Math.abs(it.width.toFloat() / it.height - jpeg.width.toFloat() / jpeg.height) * 10 - it.width / 1e4f }
            ?: Size(1280, 720)
        st.setDefaultBufferSize(preview.width, preview.height)
        val pSurface = Surface(st)
        val r = ImageReader.newInstance(jpeg.width, jpeg.height, ImageFormat.JPEG, 2)
        reader = r
        val orientation = ch.get(CameraCharacteristics.SENSOR_ORIENTATION) ?: 90

        r.setOnImageAvailableListener({ ir ->
            ir.acquireLatestImage()?.use { img ->
                val buf = img.planes[0].buffer
                val bytes = ByteArray(buf.remaining()).also { buf.get(it) }
                save(bytes)
            }
            finishSoon()
        }, bg)

        try {
            cm.openCamera(id, exec, object : CameraDevice.StateCallback() {
                override fun onOpened(d: CameraDevice) {
                    device = d
                    val outs = listOf(OutputConfiguration(pSurface), OutputConfiguration(r.surface))
                    d.createCaptureSession(SessionConfiguration(SessionConfiguration.SESSION_REGULAR, outs, exec,
                        object : CameraCaptureSession.StateCallback() {
                            override fun onConfigured(s: CameraCaptureSession) {
                                val pr = d.createCaptureRequest(CameraDevice.TEMPLATE_PREVIEW).apply {
                                    addTarget(pSurface)
                                    set(CaptureRequest.CONTROL_AF_MODE, CaptureRequest.CONTROL_AF_MODE_CONTINUOUS_PICTURE)
                                    set(CaptureRequest.CONTROL_AE_MODE, CaptureRequest.CONTROL_AE_MODE_ON)
                                }.build()
                                s.setRepeatingRequest(pr, null, bg)
                                bg.postDelayed({
                                    val still = d.createCaptureRequest(CameraDevice.TEMPLATE_STILL_CAPTURE).apply {
                                        addTarget(r.surface)
                                        set(CaptureRequest.CONTROL_AF_MODE, CaptureRequest.CONTROL_AF_MODE_CONTINUOUS_PICTURE)
                                        set(CaptureRequest.CONTROL_AE_MODE, CaptureRequest.CONTROL_AE_MODE_ON)
                                        set(CaptureRequest.JPEG_ORIENTATION, orientation)
                                        set(CaptureRequest.JPEG_QUALITY, 95.toByte())
                                    }.build()
                                    s.capture(still, null, bg)
                                }, 900)
                            }
                            override fun onConfigureFailed(s: CameraCaptureSession) = fail("session failed")
                        }))
                }
                override fun onDisconnected(d: CameraDevice) { d.close() }
                override fun onError(d: CameraDevice, e: Int) { d.close(); fail("camera error $e") }
            })
        } catch (e: Exception) { fail(e.toString()) }
    }

    private fun save(jpeg: ByteArray) {
        val name = "PS_" + SimpleDateFormat("yyyyMMdd_HHmmss", Locale.US).format(Date()) + ".jpg"
        val cv = ContentValues().apply {
            put(MediaStore.Images.Media.DISPLAY_NAME, name)
            put(MediaStore.Images.Media.MIME_TYPE, "image/jpeg")
            put(MediaStore.Images.Media.RELATIVE_PATH, "DCIM/Symphone")
            put(MediaStore.Images.Media.DATE_TAKEN, System.currentTimeMillis())
            put(MediaStore.Images.Media.IS_PENDING, 1)
        }
        val cr = contentResolver
        val uri = cr.insert(MediaStore.Images.Media.EXTERNAL_CONTENT_URI, cv) ?: return
        cr.openOutputStream(uri)?.use { it.write(jpeg) }
        cr.update(uri, ContentValues().apply { put(MediaStore.Images.Media.IS_PENDING, 0) }, null, null)
        Hub.emit("captured", JSONObject().put("id", uri.lastPathSegment?.toLongOrNull() ?: -1).put("name", name))
    }

    private fun fail(msg: String) {
        Hub.emit("captured", JSONObject().put("error", msg))
        finishSoon()
    }

    private fun finishSoon() = runOnUiThread { if (!isFinishing) finish() }

    override fun onDestroy() {
        try { device?.close() } catch (_: Exception) {}
        reader?.close()
        thread.quitSafely()
        super.onDestroy()
    }
}

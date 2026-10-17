package dev.phonesync

import android.content.Context
import android.hardware.camera2.CameraCharacteristics
import android.hardware.camera2.CameraManager
import android.os.SystemClock
import android.view.OrientationEventListener
import org.json.JSONArray
import org.json.JSONObject
import kotlin.math.atan

/**
 * Cameras for the PC's virtual webcam: which ones can be opened (ids as scrcpy uses them, with a friendly label from
 * the lens' field of view), and how to rotate a camera's frames so they are upright for the phone's current physical
 * orientation (the Camera2 "JPEG orientation" formula). The orientation sensor only runs while the PC asks for it.
 */
object Cameras {
    private val cm by lazy { Hub.ctx.getSystemService(Context.CAMERA_SERVICE) as CameraManager }

    fun list(): JSONArray {
        val out = JSONArray()
        val ids = try { cm.cameraIdList } catch (_: Exception) { emptyArray() }
        val backs = ids.count { facing(it) == "back" }
        for (id in ids) {
            val c = try { cm.getCameraCharacteristics(id) } catch (_: Exception) { continue }
            val f = facing(id)
            val fov = fov(c)
            val label = when {
                f == "front" -> "Front"
                f == "external" -> "External"
                backs <= 1 -> "Back"
                fov != null && fov > 95 -> "Back · ultra-wide"
                fov != null && fov < 45 -> "Back · telephoto"
                else -> "Back · main"
            }
            out.put(JSONObject().put("id", id).put("facing", f).put("label", label)
                .put("fov", fov?.let { Math.round(it) } ?: JSONObject.NULL)
                .put("sensorOrientation", c.get(CameraCharacteristics.SENSOR_ORIENTATION) ?: 0))
        }
        return out
    }

    private fun facing(id: String): String = try {
        when (cm.getCameraCharacteristics(id).get(CameraCharacteristics.LENS_FACING)) {
            CameraCharacteristics.LENS_FACING_FRONT -> "front"
            CameraCharacteristics.LENS_FACING_EXTERNAL -> "external"
            else -> "back"
        }
    } catch (_: Exception) { "back" }

    /** Horizontal field of view in degrees (long side of the sensor). */
    private fun fov(c: CameraCharacteristics): Double? {
        val size = c.get(CameraCharacteristics.SENSOR_INFO_PHYSICAL_SIZE) ?: return null
        val focal = c.get(CameraCharacteristics.LENS_INFO_AVAILABLE_FOCAL_LENGTHS)?.minOrNull() ?: return null
        if (focal <= 0f) return null
        return Math.toDegrees(2 * atan(maxOf(size.width, size.height) / (2.0 * focal)))
    }

    // ---- physical orientation (only while the PC is using the camera)
    @Volatile private var degrees = 0                 // device rotation, clockwise from natural, multiple of 90
    @Volatile private var lastAsked = 0L
    private var listener: OrientationEventListener? = null
    private val main = android.os.Handler(android.os.Looper.getMainLooper())
    private val idleCheck = object : Runnable {
        override fun run() {
            if (SystemClock.elapsedRealtime() - lastAsked > 15_000) { listener?.disable(); listener = null }
            else main.postDelayed(this, 5_000)
        }
    }

    private fun watch() {
        lastAsked = SystemClock.elapsedRealtime()
        if (listener != null) return
        main.post {
            if (listener != null) return@post
            val l = object : OrientationEventListener(Hub.ctx) {
                override fun onOrientationChanged(o: Int) {
                    if (o == ORIENTATION_UNKNOWN) return          // lying flat: keep the last one
                    degrees = ((o + 45) / 90 * 90) % 360
                }
            }
            if (l.canDetectOrientation()) l.enable()
            listener = l
            main.postDelayed(idleCheck, 5_000)
        }
    }

    /** Clockwise rotation that makes camera [id]'s frames upright now, and the device rotation itself. */
    fun orientation(id: String?): JSONObject {
        watch()
        val cid = id?.takeIf { it.isNotEmpty() } ?: try { cm.cameraIdList.firstOrNull() } catch (_: Exception) { null }
        val c = cid?.let { try { cm.getCameraCharacteristics(it) } catch (_: Exception) { null } }
        val sensor = c?.get(CameraCharacteristics.SENSOR_ORIENTATION) ?: 90
        val front = c?.get(CameraCharacteristics.LENS_FACING) == CameraCharacteristics.LENS_FACING_FRONT
        val dev = if (front) -degrees else degrees
        return JSONObject().put("id", cid ?: JSONObject.NULL).put("device", degrees).put("sensor", sensor)
            .put("rotation", (sensor + dev + 360) % 360)
    }
}

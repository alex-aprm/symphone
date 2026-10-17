package dev.phonesync

import android.app.Activity
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.UiModeManager
import android.bluetooth.BluetoothManager
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.location.LocationManager
import android.media.AudioManager
import android.net.wifi.WifiManager
import android.nfc.NfcManager
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.PowerManager
import android.provider.Settings
import android.service.quicksettings.TileService
import android.util.Log
import android.widget.Toast
import org.json.JSONObject

/**
 * Quick settings for the PC without adb (GET/POST /api/controls, same shape as the PC's adb version):
 * brightness, auto-brightness and auto-rotate need "Change system settings", Do not disturb needs "Do Not Disturb
 * access"; volumes and the torch need nothing. Wi-Fi, Bluetooth, mobile data, airplane mode, location, NFC, battery
 * saver and dark mode can't be switched by apps since Android 10: setting them opens the phone's own panel instead.
 */
object Controls {
    private val ctx get() = Hub.ctx
    private val STREAMS = linkedMapOf("media" to AudioManager.STREAM_MUSIC, "ring" to AudioManager.STREAM_RING,
        "alarm" to AudioManager.STREAM_ALARM, "call" to AudioManager.STREAM_VOICE_CALL)
    private val LABELS = linkedMapOf("wifi" to "Wi‑Fi", "bluetooth" to "Bluetooth", "data" to "Mobile data",
        "airplane" to "Airplane mode", "dnd" to "Do not disturb", "location" to "Location", "rotate" to "Auto-rotate",
        "saver" to "Battery saver", "dark" to "Dark mode", "nfc" to "NFC", "autobright" to "Auto brightness")

    private fun sys(k: String, def: Int = 0) = try { Settings.System.getInt(ctx.contentResolver, k) } catch (_: Exception) { def }
    private fun glob(k: String, def: Int = 0) = try { Settings.Global.getInt(ctx.contentResolver, k) } catch (_: Exception) { def }

    private fun state(k: String): Boolean? = try {
        when (k) {
            "wifi" -> ctx.getSystemService(WifiManager::class.java).isWifiEnabled
            "bluetooth" -> ctx.getSystemService(BluetoothManager::class.java)?.adapter?.isEnabled
            "data" -> glob("mobile_data", 1) == 1
            "airplane" -> glob(Settings.Global.AIRPLANE_MODE_ON) == 1
            "dnd" -> ctx.getSystemService(NotificationManager::class.java).currentInterruptionFilter != NotificationManager.INTERRUPTION_FILTER_ALL
            "location" -> ctx.getSystemService(LocationManager::class.java).isLocationEnabled
            "rotate" -> sys(Settings.System.ACCELEROMETER_ROTATION) == 1
            "saver" -> ctx.getSystemService(PowerManager::class.java).isPowerSaveMode
            "dark" -> ctx.getSystemService(UiModeManager::class.java).nightMode == UiModeManager.MODE_NIGHT_YES ||
                (ctx.resources.configuration.uiMode and android.content.res.Configuration.UI_MODE_NIGHT_MASK) == android.content.res.Configuration.UI_MODE_NIGHT_YES
            "nfc" -> ctx.getSystemService(NfcManager::class.java)?.defaultAdapter?.isEnabled
            "autobright" -> sys(Settings.System.SCREEN_BRIGHTNESS_MODE) == Settings.System.SCREEN_BRIGHTNESS_MODE_AUTOMATIC
            else -> null
        }
    } catch (_: Exception) { null }

    fun read(): JSONObject {
        val toggles = JSONObject()
        for ((k, label) in LABELS) {
            toggles.put(k, JSONObject().put("label", label).put("on", state(k) == true).put("settable", settable(k)))
        }
        val am = ctx.getSystemService(AudioManager::class.java)
        val vols = JSONObject()
        for ((k, s) in STREAMS) vols.put(k, JSONObject().put("value", am.getStreamVolume(s))
            .put("min", if (Build.VERSION.SDK_INT >= 28) am.getStreamMinVolume(s) else 0).put("max", am.getStreamMaxVolume(s)))
        return JSONObject().put("toggles", toggles).put("brightness", sys(Settings.System.SCREEN_BRIGHTNESS, 128))
            .put("volumes", vols).put("torch", Torch.on).put("adb", false)
    }

    /** Can be switched right here (with the permission granted), not just by opening a panel. */
    private fun settable(k: String) = when (k) {
        "rotate", "autobright" -> Settings.System.canWrite(ctx)
        "dnd" -> ctx.getSystemService(NotificationManager::class.java).isNotificationPolicyAccessGranted
        else -> false
    }

    fun write(b: JSONObject): JSONObject {
        val res = JSONObject().put("ok", true)
        if (b.has("toggle")) {
            val k = b.optString("toggle"); val on = b.optBoolean("on")
            when (k) {
                "torch" -> Torch.set(on)
                "rotate", "autobright" -> if (Settings.System.canWrite(ctx)) {
                    if (k == "rotate") Settings.System.putInt(ctx.contentResolver, Settings.System.ACCELEROMETER_ROTATION, if (on) 1 else 0)
                    else Settings.System.putInt(ctx.contentResolver, Settings.System.SCREEN_BRIGHTNESS_MODE,
                        if (on) Settings.System.SCREEN_BRIGHTNESS_MODE_AUTOMATIC else Settings.System.SCREEN_BRIGHTNESS_MODE_MANUAL)
                } else return res.put("ok", false).put("needs", "write_settings")
                    .put("error", "Allow 'Change system settings' for Symphone on the phone")
                "dnd" -> {
                    val nm = ctx.getSystemService(NotificationManager::class.java)
                    if (!nm.isNotificationPolicyAccessGranted) return res.put("ok", false).put("needs", "dnd")
                        .put("error", "Allow 'Do Not Disturb access' for Symphone on the phone")
                    nm.setInterruptionFilter(if (on) NotificationManager.INTERRUPTION_FILTER_PRIORITY else NotificationManager.INTERRUPTION_FILTER_ALL)
                }
                else -> res.put("opened", openPanel(k))
            }
        }
        if (b.has("brightness")) {
            if (!Settings.System.canWrite(ctx)) return res.put("ok", false).put("needs", "write_settings")
                .put("error", "Allow 'Change system settings' for Symphone on the phone")
            Settings.System.putInt(ctx.contentResolver, Settings.System.SCREEN_BRIGHTNESS_MODE, Settings.System.SCREEN_BRIGHTNESS_MODE_MANUAL)
            Settings.System.putInt(ctx.contentResolver, Settings.System.SCREEN_BRIGHTNESS, b.optInt("brightness").coerceIn(1, 255))
        }
        if (b.has("volume")) {
            val s = STREAMS[b.optString("volume")] ?: return res.put("ok", false).put("error", "unknown stream")
            try { ctx.getSystemService(AudioManager::class.java).setStreamVolume(s, b.optInt("value"), 0) }
            catch (e: SecurityException) { return res.put("ok", false).put("needs", "dnd").put("error", "Android needs Do Not Disturb access to change this volume") }
        }
        return res
    }

    /** The phone's own switch for something apps can't toggle. */
    private fun openPanel(k: String): Boolean {
        val action = when (k) {
            "wifi" -> if (Build.VERSION.SDK_INT >= 29) Settings.Panel.ACTION_WIFI else Settings.ACTION_WIFI_SETTINGS
            "data" -> if (Build.VERSION.SDK_INT >= 29) Settings.Panel.ACTION_INTERNET_CONNECTIVITY else Settings.ACTION_DATA_ROAMING_SETTINGS
            "nfc" -> if (Build.VERSION.SDK_INT >= 29) Settings.Panel.ACTION_NFC else Settings.ACTION_NFC_SETTINGS
            "bluetooth" -> Settings.ACTION_BLUETOOTH_SETTINGS
            "airplane" -> Settings.ACTION_AIRPLANE_MODE_SETTINGS
            "location" -> Settings.ACTION_LOCATION_SOURCE_SETTINGS
            "saver" -> Settings.ACTION_BATTERY_SAVER_SETTINGS
            "dark" -> Settings.ACTION_DISPLAY_SETTINGS
            else -> return false
        }
        var ok = true
        Handler(Looper.getMainLooper()).post {
            try { ctx.startActivity(Intent(action).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)) }
            catch (e: Exception) { ok = false; Log.w("Symphone", "panel $k: $e") }
        }
        return ok
    }
}

/** Reads the clipboard (only possible while in the foreground) and sends it to the main PC. */
class ClipSendActivity : Activity() {
    private var sent = false

    override fun onWindowFocusChanged(hasFocus: Boolean) {
        super.onWindowFocusChanged(hasFocus)
        if (!hasFocus || sent) return
        sent = true
        val cm = getSystemService(ClipboardManager::class.java)
        val text = try { cm.primaryClip?.takeIf { it.itemCount > 0 }?.getItemAt(0)?.coerceToText(this)?.toString() } catch (_: Exception) { null }
        val pc = Pcs.master
        val msg = when {
            text.isNullOrEmpty() -> "The clipboard is empty"
            pc == null -> "Pair with a PC first"
            Hub.sendClip(text) -> "Clipboard sent to ${pc.name}"
            else -> "Couldn't reach ${pc.name}"
        }
        Toast.makeText(this, msg, Toast.LENGTH_SHORT).show()
        finish()
        overridePendingTransition(0, 0)
    }

    override fun onCreate(b: Bundle?) { super.onCreate(b) }
}

/** Quick Settings tile "Send clipboard to PC". */
class ClipTileService : TileService() {
    override fun onClick() {
        val i = Intent(this, ClipSendActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        if (Build.VERSION.SDK_INT >= 34) {
            startActivityAndCollapse(PendingIntent.getActivity(this, 0, i, PendingIntent.FLAG_IMMUTABLE))
        } else {
            @Suppress("DEPRECATION", "StartActivityAndCollapseDeprecated")
            startActivityAndCollapse(i)
        }
    }
}

package dev.phonesync

import android.Manifest
import android.app.Activity
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Environment
import android.os.PowerManager
import android.provider.Settings
import org.json.JSONArray
import org.json.JSONObject

/**
 * The setup checklist: everything Symphone needs from Android, with a live status, why it is needed and how to fix
 * it (a permission dialog when Android allows one, otherwise the exact Settings screen plus short steps).
 * Used by [SetupActivity], the home screen and GET /api/setup (so the PC can show the phone's checklist).
 */
object Setup {
    /** ok: true / false, or null when Android can't tell us (vendor autostart switches). */
    data class Item(val id: String, val title: String, val why: String, val required: Boolean, val ok: Boolean?,
                    val hint: String? = null, val action: String = "Allow")

    private val prefs get() = Hub.prefs

    var done: Boolean
        get() = prefs.getBoolean("setupDone", false)
        set(v) { prefs.edit().putBoolean("setupDone", v).apply() }

    fun items(ctx: Context): List<Item> {
        val out = ArrayList<Item>()
        out += Item("paired", "Connect to your PC", "Pair with Symphone on Windows (QR code or 6-digit code)",
            true, !Pcs.isEmpty, action = "Pair")
        if (Build.VERSION.SDK_INT >= 33)
            out += Item("notifications", "Show notifications", "Lets Symphone keep running and tell you about transfers",
                false, granted(ctx, Manifest.permission.POST_NOTIFICATIONS), deniedHint(ctx, "notifications"))
        val notif = notifAccess(ctx)
        out += Item("notif_access", "Notification access", "Shows your phone's notifications on the PC, with replies and actions",
            true, notif, if (restricted(ctx)) RESTRICTED_STEPS else null,
            action = if (restricted(ctx) && nextIsAppInfo()) "Open App info" else "Allow")
        out += Item("all_files", "All files access", "Your photos and files on the PC (Explorer, Photos, Files)",
            true, Environment.isExternalStorageManager())
        out += Item("battery", "Run in the background", "Keeps the connection to your PC alive (unrestricted battery)",
            true, ctx.getSystemService(PowerManager::class.java).isIgnoringBatteryOptimizations(ctx.packageName))
        out += Item("overlay", "Open apps from the PC", "Lets the PC open apps and notifications on the phone (display over other apps)",
            false, Settings.canDrawOverlays(ctx), "Android opens the full list: find Symphone in it and turn it on.")
        out += Item("camera", "Camera", "Use the phone as a webcam and take photos from the PC",
            false, granted(ctx, Manifest.permission.CAMERA), deniedHint(ctx, "camera"))
        out += Item("calls", "Calls", "Answer and decline calls from the PC",
            false, CALLS.all { granted(ctx, it) }, deniedHint(ctx, "calls"))
        if (Build.VERSION.SDK_INT >= 31 && Ble.supported)
            out += Item("nearby", "Nearby devices", "Finds your PC over Bluetooth, even before Wi-Fi connects",
                false, Ble.hasPermissions(), deniedHint(ctx, "nearby"))
        vendorHint()?.let { out += Item("autostart", "Allow auto-launch", "Your phone's maker can stop apps in the background",
            false, null, it, action = "Open settings") }
        val wd = wirelessDebugging(ctx)
        out += Item("wireless_debugging", "Wireless debugging", "For phone apps in PC windows, the phone screen, the PC keyboard and the clipboard from the phone",
            false, wd, if (wd) null else WIRELESS_DEBUGGING_STEPS, action = if (devOptions(ctx)) "Developer options" else "About phone")
        return out
    }

    fun json(ctx: Context): JSONObject {
        val arr = JSONArray()
        for (i in items(ctx)) arr.put(JSONObject().put("id", i.id).put("title", i.title).put("why", i.why)
            .put("ok", i.ok ?: JSONObject.NULL).put("required", i.required).put("hint", i.hint ?: JSONObject.NULL))
        return JSONObject().put("items", arr).put("done", done).put("requiredOk", requiredOk(ctx))
            .put("installer", installer(ctx) ?: JSONObject.NULL)
    }

    fun requiredOk(ctx: Context) = items(ctx).filter { it.required }.all { it.ok == true }

    // ------------------------------------------------------------------ actions
    const val REQ_RUNTIME = 41

    fun act(a: Activity, id: String) {
        val pkg = a.packageName
        fun open(i: Intent) = try { a.startActivity(i); true } catch (_: Exception) { false }
        fun appInfo() = open(Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:$pkg")))
        when (id) {
            "paired" -> a.startActivity(Intent(a, ScanActivity::class.java))
            "notifications" -> runtime(a, id, arrayOf(Manifest.permission.POST_NOTIFICATIONS))
            "camera" -> runtime(a, id, arrayOf(Manifest.permission.CAMERA))
            "calls" -> runtime(a, id, CALLS)
            "nearby" -> if (Build.VERSION.SDK_INT >= 31)
                runtime(a, id, arrayOf(Manifest.permission.BLUETOOTH_ADVERTISE, Manifest.permission.BLUETOOTH_CONNECT))
            "notif_access" -> {
                if (restricted(a) && nextIsAppInfo()) { prefs.edit().putString("notifLast", "appinfo").apply(); appInfo(); return }
                prefs.edit().putBoolean("notifTried", true).putString("notifLast", "listener").apply()
                open(Intent(Settings.ACTION_NOTIFICATION_LISTENER_DETAIL_SETTINGS)
                    .putExtra(Settings.EXTRA_NOTIFICATION_LISTENER_COMPONENT_NAME,
                        ComponentName(a, NotifListener::class.java).flattenToString()))
                    || open(Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS))
            }
            "all_files" -> open(Intent(Settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION, Uri.parse("package:$pkg")))
                || open(Intent(Settings.ACTION_MANAGE_ALL_FILES_ACCESS_PERMISSION))
            "battery" -> open(Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:$pkg")))
                || open(Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS))
            "overlay" -> open(Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION, Uri.parse("package:$pkg")))
            "autostart" -> if (vendorIntents().none { open(it) }) appInfo()
            "wireless_debugging" -> open(Intent(if (devOptions(a)) Settings.ACTION_APPLICATION_DEVELOPMENT_SETTINGS
                else Settings.ACTION_DEVICE_INFO_SETTINGS)) || open(Intent(Settings.ACTION_SETTINGS))
        }
    }

    /** Runtime permissions: the system dialog, or App info once Android stops showing it ("Don't ask again"). */
    private fun runtime(a: Activity, id: String, perms: Array<String>) {
        val missing = perms.filter { a.checkSelfPermission(it) != PackageManager.PERMISSION_GRANTED }
        if (missing.isEmpty()) return
        val asked = prefs.getBoolean("asked_$id", false)
        if (asked && missing.none { a.shouldShowRequestPermissionRationale(it) }) {
            a.startActivity(Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:${a.packageName}")))
            return
        }
        prefs.edit().putBoolean("asked_$id", true).apply()
        a.requestPermissions(missing.toTypedArray(), REQ_RUNTIME)
    }

    private fun deniedHint(ctx: Context, id: String): String? {
        if (!prefs.getBoolean("asked_$id", false)) return null
        val a = ctx as? Activity ?: return null
        val perms = when (id) {
            "notifications" -> if (Build.VERSION.SDK_INT >= 33) arrayOf(Manifest.permission.POST_NOTIFICATIONS) else return null
            "camera" -> arrayOf(Manifest.permission.CAMERA)
            "calls" -> CALLS
            "nearby" -> if (Build.VERSION.SDK_INT >= 31) arrayOf(Manifest.permission.BLUETOOTH_CONNECT) else return null
            else -> return null
        }
        val missing = perms.filter { !granted(ctx, it) }
        return if (missing.isNotEmpty() && missing.none { a.shouldShowRequestPermissionRationale(it) })
            "Android won't ask again. Open App info › Permissions and allow it there." else null
    }

    // ------------------------------------------------------------------ checks
    private val CALLS = arrayOf(Manifest.permission.READ_PHONE_STATE, Manifest.permission.ANSWER_PHONE_CALLS)

    private fun granted(ctx: Context, p: String) = ctx.checkSelfPermission(p) == PackageManager.PERMISSION_GRANTED

    fun notifAccess(ctx: Context) =
        Settings.Secure.getString(ctx.contentResolver, "enabled_notification_listeners")?.contains(ctx.packageName) == true

    /** Who installed us (com.android.vending: Play Store; a package installer: an APK file; null: adb). */
    fun installer(ctx: Context): String? = try {
        if (Build.VERSION.SDK_INT >= 30) ctx.packageManager.getInstallSourceInfo(ctx.packageName).installingPackageName
        else @Suppress("DEPRECATION") ctx.packageManager.getInstallerPackageName(ctx.packageName)
    } catch (_: Exception) { null }

    /**
     * Android 13+ blocks notification access (a "restricted setting") for apps installed from an APK file until the
     * user allows it in App info; apps can't query that state. So: the first tap opens the notification access
     * screen; if the user comes back without access, the steps are shown and the button alternates between App info
     * (to allow restricted settings) and the access screen.
     */
    fun restricted(ctx: Context): Boolean =
        Build.VERSION.SDK_INT >= 33 && !notifAccess(ctx) && prefs.getBoolean("notifTried", false)

    private fun nextIsAppInfo() = prefs.getString("notifLast", "listener") == "listener"

    private fun devOptions(ctx: Context) =
        Settings.Global.getInt(ctx.contentResolver, Settings.Global.DEVELOPMENT_SETTINGS_ENABLED, 0) == 1

    fun wirelessDebugging(ctx: Context) = Settings.Global.getInt(ctx.contentResolver, "adb_wifi_enabled", 0) == 1

    private const val RESTRICTED_STEPS = "Android blocks this for apps installed from a file until you allow it: " +
        "open App info › ⋮ (top right) › Allow restricted settings, confirm, then come back and tap Allow again."

    private const val WIRELESS_DEBUGGING_STEPS = "Optional. Settings › About phone › tap Build number 7 times to unlock " +
        "Developer options. Then Developer options › Wireless debugging › On › Pair device with QR code, and scan the " +
        "code Symphone shows on your PC."

    // ------------------------------------------------------------------ phone makers that stop background apps
    private val maker get() = Build.MANUFACTURER.lowercase()

    private fun vendorHint(): String? = when {
        maker.contains("nubia") || maker.contains("zte") -> "Settings › Apps › Symphone: turn on Auto-launch and set Battery to Unrestricted."
        maker.contains("xiaomi") || maker.contains("redmi") || maker.contains("poco") -> "Turn on Autostart, and set Battery saver to No restrictions."
        maker.contains("huawei") || maker.contains("honor") -> "App launch › Symphone: choose Manage manually and allow all three switches."
        maker.contains("oppo") || maker.contains("realme") || maker.contains("oneplus") -> "Allow Auto launch and Background activity for Symphone."
        maker.contains("vivo") || maker.contains("iqoo") -> "Allow Autostart and High background power consumption for Symphone."
        maker.contains("samsung") -> "Battery › Background usage limits › Never sleeping apps: add Symphone."
        else -> null
    }

    private fun vendorIntents(): List<Intent> {
        fun c(pkg: String, cls: String) = Intent().setComponent(ComponentName(pkg, cls)).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        return when {
            maker.contains("xiaomi") || maker.contains("redmi") || maker.contains("poco") ->
                listOf(c("com.miui.securitycenter", "com.miui.permcenter.autostart.AutoStartManagementActivity"))
            maker.contains("huawei") || maker.contains("honor") -> listOf(
                c("com.huawei.systemmanager", "com.huawei.systemmanager.startupmgr.ui.StartupNormalAppListActivity"),
                c("com.huawei.systemmanager", "com.huawei.systemmanager.optimize.process.ProtectActivity"))
            maker.contains("oppo") || maker.contains("realme") -> listOf(
                c("com.coloros.safecenter", "com.coloros.safecenter.permission.startup.StartupAppListActivity"),
                c("com.coloros.safecenter", "com.coloros.safecenter.startupapp.StartupAppListActivity"),
                c("com.oplus.safecenter", "com.oplus.safecenter.startupapp.view.StartupAppListActivity"))
            maker.contains("oneplus") -> listOf(
                c("com.oneplus.security", "com.oneplus.security.chainlaunch.view.ChainLaunchAppListActivity"))
            maker.contains("vivo") || maker.contains("iqoo") -> listOf(
                c("com.vivo.permissionmanager", "com.vivo.permissionmanager.activity.BgStartUpManagerActivity"),
                c("com.iqoo.secure", "com.iqoo.secure.ui.phoneoptimize.BgStartUpManager"))
            maker.contains("samsung") -> listOf(
                c("com.samsung.android.lool", "com.samsung.android.sm.battery.ui.BatteryActivity"))
            else -> emptyList()                      // nubia/ZTE and others: App info (see the hint)
        }
    }
}

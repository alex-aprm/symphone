package dev.phonesync

import android.Manifest
import android.app.Activity
import android.app.AlertDialog
import android.content.ComponentName
import android.content.Intent
import android.content.pm.PackageManager
import android.content.res.ColorStateList
import android.graphics.Typeface
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Environment
import android.os.Handler
import android.os.Looper
import android.os.PowerManager
import android.provider.Settings
import android.text.format.DateUtils
import android.view.Gravity
import android.view.View
import android.widget.LinearLayout
import android.widget.PopupMenu
import android.widget.RadioButton
import android.widget.ScrollView
import android.widget.Switch
import android.widget.TextView
import java.lang.ref.WeakReference

/** Home screen: this phone, paired computers, pairing (QR / code), clipboard toggle and the permission checklist. */
class MainActivity : Activity() {
    private lateinit var ui: Ui
    private lateinit var status: TextView
    private lateinit var pcList: LinearLayout
    private lateinit var code: TextView
    private lateinit var clipSwitch: Switch
    private lateinit var perms: LinearLayout
    private val handler = Handler(Looper.getMainLooper())
    private val ticker = object : Runnable {
        override fun run() { render(); handler.postDelayed(this, 5000) }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        requestWindowFeature(android.view.Window.FEATURE_NO_TITLE)
        current = WeakReference(this)
        Hub.startService(this)
        ui = Ui(this)
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(ui.dp(16), ui.dp(24), ui.dp(16), ui.dp(24))
        }

        // Header
        root.addView(ui.text("Symphone", 14f, ui.primary, bold = true), ui.lp(bottom = 2))
        root.addView(ui.text(Hub.deviceName, 30f, bold = true), ui.lp())
        status = ui.text("", 14f, ui.onSurfaceVariant)
        root.addView(status, ui.lp(top = 4, bottom = 20))

        // Computers
        root.addView(section("Computers"))
        pcList = ui.card().apply { setPadding(ui.dp(8), ui.dp(8), ui.dp(8), ui.dp(8)) }
        root.addView(pcList, ui.lp(bottom = 16))

        root.addView(ui.button("Scan QR code") { startActivity(Intent(this, ScanActivity::class.java)) }.apply {
            textSize = 17f; setPadding(ui.dp(24), ui.dp(18), ui.dp(24), ui.dp(18))
        }, ui.lp(bottom = 16))

        // Pair with code
        val codeCard = ui.card()
        codeCard.addView(ui.text("Pair with code", 16f, bold = true))
        codeCard.addView(ui.text("On the PC choose \"Pair with code\" and enter:", 14f, ui.onSurfaceVariant), ui.lp(top = 2, bottom = 8))
        code = ui.text("", 40f, ui.primary).apply { typeface = Typeface.MONOSPACE; letterSpacing = 0.08f }
        codeCard.addView(code)
        root.addView(codeCard, ui.lp(bottom = 16))

        // Clipboard toggle
        val clipCard = ui.card()
        val clipRow = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL }
        val clipTexts = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        clipTexts.addView(ui.text("Share clipboard with PC", 16f, bold = true))
        clipTexts.addView(ui.text("Text you copy here is sent to the master PC", 13f, ui.onSurfaceVariant))
        clipRow.addView(clipTexts, LinearLayout.LayoutParams(0, -2, 1f))
        clipSwitch = Switch(this).apply {
            isChecked = Hub.clipShare
            thumbTintList = ColorStateList(arrayOf(intArrayOf(android.R.attr.state_checked), intArrayOf()), intArrayOf(ui.primary, ui.outline))
            trackTintList = ColorStateList(arrayOf(intArrayOf(android.R.attr.state_checked), intArrayOf()), intArrayOf(ui.primaryContainer, ui.surface))
            setOnCheckedChangeListener { _, on -> Hub.clipShare = on }
        }
        clipRow.addView(clipSwitch)
        clipCard.addView(clipRow)
        root.addView(clipCard, ui.lp(bottom = 16))

        // Permissions
        root.addView(section("Permissions"))
        perms = ui.card().apply { setPadding(ui.dp(16), ui.dp(8), ui.dp(16), ui.dp(8)) }
        root.addView(perms, ui.lp(bottom = 16))

        val scroll = ScrollView(this).apply { addView(root); setBackgroundColor(ui.surface); isFillViewport = true }
        setContentView(scroll)
        ui.applyWindow(this, root)

        val want = ArrayList<String>()
        if (Build.VERSION.SDK_INT >= 33 && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            want += Manifest.permission.POST_NOTIFICATIONS
            want += listOf(Manifest.permission.READ_PHONE_STATE, Manifest.permission.ANSWER_PHONE_CALLS, Manifest.permission.CAMERA)
        }
        if (Build.VERSION.SDK_INT >= 31 && Ble.supported && !Ble.hasPermissions()) {
            want += listOf(Manifest.permission.BLUETOOTH_ADVERTISE, Manifest.permission.BLUETOOTH_CONNECT)
        }
        if (want.isNotEmpty()) requestPermissions(want.toTypedArray(), 1)
    }

    override fun onResume() {
        super.onResume()
        clipSwitch.isChecked = Hub.clipShare
        handler.removeCallbacks(ticker)
        handler.post(ticker)
    }

    override fun onPause() {
        handler.removeCallbacks(ticker)
        super.onPause()
    }

    override fun onDestroy() {
        if (current?.get() === this) current = null
        super.onDestroy()
    }

    private fun section(title: String) = ui.text(title, 14f, ui.primary, bold = true).apply {
        setPadding(ui.dp(8), 0, 0, ui.dp(8))
    }

    private fun render() {
        val ip = Arrival.currentIp()
        val online = Pcs.all().count { it.online }
        status.text = listOfNotNull(
            if (ip != null) "Wi-Fi $ip" else if (WfdClient.network != null) "Direct Wi-Fi to PC" else "No Wi-Fi",
            if (Pcs.isEmpty) "not paired" else "$online of ${Pcs.all().size} computer${if (Pcs.all().size == 1) "" else "s"} online",
            if (Ble.advertising) "Bluetooth on" else null,
        ).joinToString("  ·  ")
        code.text = Hub.pairCode.chunked(3).joinToString(" ")
        renderPcs()
        renderPerms()
    }

    private fun renderPcs() {
        pcList.removeAllViews()
        val pcs = Pcs.all()
        if (pcs.isEmpty()) {
            pcList.addView(ui.text("No computers yet. Open Symphone on your PC, choose Settings → Add phone and scan the QR code.",
                14f, ui.onSurfaceVariant).apply { setPadding(ui.dp(12), ui.dp(12), ui.dp(12), ui.dp(12)) })
            return
        }
        for (pc in pcs) pcList.addView(pcRow(pc), ui.lp())
    }

    private fun pcRow(pc: Pc): View {
        val row = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL
            setPadding(ui.dp(12), ui.dp(10), ui.dp(4), ui.dp(10))
            background = ui.ripple(null, 16)
            isClickable = true
            setOnClickListener { if (!pc.master) { Pcs.setMaster(pc.id); render() } }
            setOnLongClickListener { confirmRemove(pc); true }
        }
        row.addView(ui.dot(if (pc.online) ui.good else ui.outline, 12))
        val texts = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(ui.dp(14), 0, ui.dp(8), 0) }
        texts.addView(ui.text(pc.name, 17f, bold = true))
        val sub = buildString {
            append(if (pc.online) "Online" else if (pc.lastSeen > 0) "Seen " + DateUtils.getRelativeTimeSpanString(pc.lastSeen) else "Never connected")
            if (pc.master) append(" · Master") else append(" · Tap to make master")
        }
        texts.addView(ui.text(sub, 13f, if (pc.master) ui.primary else ui.onSurfaceVariant))
        row.addView(texts, LinearLayout.LayoutParams(0, -2, 1f))
        row.addView(RadioButton(this).apply {
            isChecked = pc.master
            buttonTintList = ColorStateList.valueOf(if (pc.master) ui.primary else ui.outline)
            contentDescription = "Master"
            setOnClickListener { Pcs.setMaster(pc.id); render() }
        })
        row.addView(TextView(this).apply {
            text = "⋮"; textSize = 22f; setTextColor(ui.onSurfaceVariant); gravity = Gravity.CENTER
            setPadding(ui.dp(10), ui.dp(4), ui.dp(10), ui.dp(4))
            background = ui.ripple(null, 20)
            contentDescription = "More options"
            setOnClickListener { v ->
                PopupMenu(this@MainActivity, v).apply {
                    if (!pc.master) menu.add(0, 1, 0, "Make master")
                    menu.add(0, 2, 1, "Remove")
                    setOnMenuItemClickListener {
                        when (it.itemId) { 1 -> { Pcs.setMaster(pc.id); render() }; 2 -> confirmRemove(pc) }
                        true
                    }
                }.show()
            }
        })
        return row
    }

    private fun confirmRemove(pc: Pc) {
        AlertDialog.Builder(this)
            .setTitle("Remove ${pc.name}?")
            .setMessage("It will no longer be able to connect to this phone until you pair again.")
            .setPositiveButton("Remove") { _, _ -> Pcs.remove(pc.id); render() }
            .setNegativeButton("Cancel", null)
            .show()
    }

    private fun renderPerms() {
        perms.removeAllViews()
        val pm = getSystemService(PowerManager::class.java)
        check("All files access", Environment.isExternalStorageManager()) {
            startActivity(Intent(Settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION, Uri.parse("package:$packageName")))
        }
        check("Notification access", notifAccess()) {
            startActivity(Intent(Settings.ACTION_NOTIFICATION_LISTENER_DETAIL_SETTINGS)
                .putExtra(Settings.EXTRA_NOTIFICATION_LISTENER_COMPONENT_NAME, ComponentName(this, NotifListener::class.java).flattenToString()))
        }
        check("Open apps from PC (display over apps)", Settings.canDrawOverlays(this)) {
            startActivity(Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION, Uri.parse("package:$packageName")))
        }
        check("Unrestricted battery", pm.isIgnoringBatteryOptimizations(packageName)) {
            startActivity(Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:$packageName")))
        }
        check("Calls & camera", granted(Manifest.permission.ANSWER_PHONE_CALLS) && granted(Manifest.permission.CAMERA)) {
            requestPermissions(arrayOf(Manifest.permission.READ_PHONE_STATE, Manifest.permission.ANSWER_PHONE_CALLS, Manifest.permission.CAMERA), 2)
        }
        if (Ble.supported) {
            check("Bluetooth (find PCs nearby)", Ble.hasPermissions()) {
                if (Build.VERSION.SDK_INT >= 31)
                    requestPermissions(arrayOf(Manifest.permission.BLUETOOTH_ADVERTISE, Manifest.permission.BLUETOOTH_CONNECT), 3)
            }
        }
    }

    private fun granted(p: String) = checkSelfPermission(p) == PackageManager.PERMISSION_GRANTED

    private fun notifAccess() =
        Settings.Secure.getString(contentResolver, "enabled_notification_listeners")?.contains(packageName) == true

    private fun check(label: String, ok: Boolean, fix: () -> Unit) {
        val row = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL
            setPadding(0, ui.dp(8), 0, ui.dp(8))
        }
        row.addView(TextView(this).apply {
            text = if (ok) "✓" else "!"
            textSize = 15f; gravity = Gravity.CENTER
            setTextColor(if (ok) ui.good else ui.bad)
            typeface = Typeface.DEFAULT_BOLD
        }, LinearLayout.LayoutParams(ui.dp(28), -2))
        row.addView(ui.text(label, 15f), LinearLayout.LayoutParams(0, -2, 1f))
        if (!ok) row.addView(ui.button("Grant", filled = false) { fix() }.apply {
            textSize = 14f; setPadding(ui.dp(16), ui.dp(8), ui.dp(16), ui.dp(8))
        })
        perms.addView(row)
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        if (Ble.hasPermissions()) Ble.refresh()
        render()
    }

    companion object {
        private var current: WeakReference<MainActivity>? = null
        fun refresh() { current?.get()?.let { a -> a.runOnUiThread { if (!a.isDestroyed) a.render() } } }
    }
}

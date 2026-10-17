package dev.phonesync

import android.app.Activity
import android.content.Context
import android.content.Intent
import android.graphics.Typeface
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.Gravity
import android.view.View
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView

/**
 * First-run setup: welcome → connect to your PC → permissions checklist → done. Opened by the launcher until setup
 * is finished; afterwards from the home screen ("Setup checklist", which starts at the checklist).
 */
class SetupActivity : Activity() {
    private lateinit var ui: Ui
    private lateinit var root: LinearLayout
    private var step = 0
    private val handler = Handler(Looper.getMainLooper())
    private val ticker = object : Runnable {
        override fun run() { refresh(); handler.postDelayed(this, 1500) }
    }
    private var lastSig = ""

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        requestWindowFeature(android.view.Window.FEATURE_NO_TITLE)
        Hub.startService(this)
        ui = Ui(this)
        step = savedInstanceState?.getInt("step") ?: intent.getIntExtra(EXTRA_STEP, 0)
        root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(ui.dp(20), ui.dp(28), ui.dp(20), ui.dp(24))
        }
        val scroll = ScrollView(this).apply { addView(root); setBackgroundColor(ui.surface); isFillViewport = true }
        setContentView(scroll)
        ui.applyWindow(this, root)
        render()
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        if (intent.hasExtra(EXTRA_STEP)) go(intent.getIntExtra(EXTRA_STEP, step))
    }

    override fun onSaveInstanceState(out: Bundle) { super.onSaveInstanceState(out); out.putInt("step", step) }

    override fun onResume() {
        super.onResume()
        lastSig = ""
        handler.removeCallbacks(ticker)
        handler.post(ticker)
    }

    override fun onPause() { handler.removeCallbacks(ticker); super.onPause() }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        if (Ble.hasPermissions()) Ble.refresh()
        lastSig = ""
        refresh()
    }

    /** Re-renders only when something changed (statuses are re-checked every 1.5 s and on return from Settings). */
    private fun refresh() {
        val sig = step.toString() + "|" + Setup.items(this).joinToString { "${it.id}${it.ok}${it.hint}" } + Hub.pairCode
        if (sig == lastSig) return
        lastSig = sig
        if (step == STEP_CONNECT && !Pcs.isEmpty) { step = STEP_PERMISSIONS }
        render()
    }

    private fun go(s: Int) { step = s; lastSig = ""; refresh() }

    private fun render() {
        root.removeAllViews()
        root.addView(dots(), ui.lp(bottom = 20))
        when (step) {
            STEP_WELCOME -> welcome()
            STEP_CONNECT -> connect()
            STEP_PERMISSIONS -> permissions()
            else -> finished()
        }
    }

    private fun dots() = LinearLayout(this).apply {
        orientation = LinearLayout.HORIZONTAL
        for (i in 0..3) addView(View(this@SetupActivity).apply {
            background = ui.rounded(if (i <= step) ui.primary else ui.secondaryContainer, 3)
        }, LinearLayout.LayoutParams(0, ui.dp(6), 1f).apply { marginEnd = if (i < 3) ui.dp(6) else 0 })
    }

    private fun title(s: String) = root.addView(ui.text(s, 28f, bold = true), ui.lp(bottom = 8))
    private fun body(s: String) = root.addView(ui.text(s, 16f, ui.onSurfaceVariant), ui.lp(bottom = 20))

    private fun primary(label: String, enabled: Boolean = true, onClick: () -> Unit) =
        root.addView(ui.button(label) { if (enabled) onClick() }.apply { alpha = if (enabled) 1f else 0.4f }, ui.lp(top = 8))

    private fun secondary(label: String, onClick: () -> Unit) =
        root.addView(ui.button(label, filled = false) { onClick() }, ui.lp(top = 8))

    private fun link(label: String, onClick: () -> Unit) = root.addView(ui.text(label, 15f, ui.primary).apply {
        gravity = Gravity.CENTER; setPadding(0, ui.dp(14), 0, ui.dp(14)); isClickable = true
        background = ui.ripple(null, 20); setOnClickListener { onClick() }
    }, ui.lp(top = 4))

    // ------------------------------------------------------------------ steps
    private fun welcome() {
        root.addView(ui.text("Symphone", 14f, ui.primary, bold = true), ui.lp(bottom = 4))
        title("Your phone, on your PC")
        body("Symphone puts your phone's apps, notifications, photos, files, clipboard, calls and camera on your " +
            "Windows PC. Setup takes about a minute.")
        val card = ui.card()
        card.addView(ui.text("You'll need Symphone running on your PC", 15f, bold = true))
        card.addView(ui.text("Download it from github.com/alex-aprm/symphone (Releases) and start it; it shows a " +
            "QR code for this phone.", 14f, ui.onSurfaceVariant), ui.lp(top = 4))
        root.addView(card, ui.lp(bottom = 12))
        primary("Get started") { go(if (Pcs.isEmpty) STEP_CONNECT else STEP_PERMISSIONS) }
    }

    private fun connect() {
        title("Connect to your PC")
        body("On the PC, open Symphone › Settings › Add a phone. Then scan the QR code it shows.")
        primary("Scan QR code") { startActivity(Intent(this, ScanActivity::class.java)) }
        root.addView(ui.text("You can also point your camera app at the QR code.", 13f, ui.onSurfaceVariant)
            .apply { gravity = Gravity.CENTER }, ui.lp(top = 8, bottom = 16))
        val code = ui.card()
        code.addView(ui.text("Or pair with a code", 15f, bold = true))
        code.addView(ui.text("Choose \"Pair with code\" on the PC and enter:", 14f, ui.onSurfaceVariant), ui.lp(top = 2, bottom = 8))
        code.addView(ui.text(Hub.pairCode.chunked(3).joinToString(" "), 36f, ui.primary).apply {
            typeface = Typeface.MONOSPACE; letterSpacing = 0.08f
        })
        root.addView(code, ui.lp(bottom = 12))
        val usb = ui.card()
        usb.addView(ui.text("Or plug in a USB cable", 15f, bold = true))
        usb.addView(ui.text("With USB debugging on, the PC pairs and sets everything up by itself.", 14f, ui.onSurfaceVariant), ui.lp(top = 2))
        root.addView(usb, ui.lp(bottom = 4))
        link("Skip for now") { go(STEP_PERMISSIONS) }
    }

    private fun permissions() {
        title("Permissions")
        body("Symphone needs a few things from Android. Each one opens the right screen; come back here when done.")
        val items = Setup.items(this)
        val req = items.filter { it.required }
        val opt = items.filter { !it.required }
        root.addView(section("Required"))
        root.addView(list(req), ui.lp(bottom = 16))
        root.addView(section("Optional"))
        root.addView(list(opt), ui.lp(bottom = 12))
        val ok = req.all { it.ok == true }
        val left = req.count { it.ok != true }
        primary(if (ok) "Done" else "$left required item${if (left == 1) "" else "s"} left", ok) { go(STEP_DONE) }
        link("Finish later") { finish() }
    }

    private fun finished() {
        Setup.done = true
        title("All set")
        val pcs = Pcs.all()
        body(if (pcs.isEmpty()) "Symphone is ready. Pair with your PC any time from the home screen."
            else "Your phone is connected to ${pcs.joinToString { it.name }}. You can close this app; Symphone keeps " +
                "working in the background.")
        primary("Open Symphone") {
            startActivity(Intent(this, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP))
            finish()
        }
    }

    private fun section(t: String) = ui.text(t, 14f, ui.primary, bold = true).apply { setPadding(ui.dp(8), 0, 0, ui.dp(8)) }

    private fun list(items: List<Setup.Item>): LinearLayout {
        val card = ui.card().apply { setPadding(ui.dp(16), ui.dp(4), ui.dp(16), ui.dp(4)) }
        items.forEachIndexed { i, it ->
            if (i > 0) card.addView(View(this).apply { setBackgroundColor((ui.outline and 0x00FFFFFF) or 0x33000000) }, ui.lp(h = 1))
            card.addView(row(this, ui, it) { Setup.act(this, it.id) })
        }
        return card
    }

    companion object {
        const val EXTRA_STEP = "step"
        const val STEP_WELCOME = 0
        const val STEP_CONNECT = 1
        const val STEP_PERMISSIONS = 2
        const val STEP_DONE = 3

        /** One checklist row: status mark, title + why (+ steps), and the fix button. Shared with the home screen. */
        fun row(ctx: Context, ui: Ui, it: Setup.Item, onFix: () -> Unit): View {
            val row = LinearLayout(ctx).apply {
                orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL
                setPadding(0, ui.dp(12), 0, ui.dp(12))
            }
            row.addView(TextView(ctx).apply {
                text = when (it.ok) { true -> "✓"; false -> if (it.required) "!" else "○"; null -> "?" }
                textSize = 16f; gravity = Gravity.CENTER; typeface = Typeface.DEFAULT_BOLD
                setTextColor(when (it.ok) { true -> ui.good; false -> if (it.required) ui.bad else ui.outline; null -> ui.outline })
            }, LinearLayout.LayoutParams(ui.dp(28), -2))
            val texts = LinearLayout(ctx).apply { orientation = LinearLayout.VERTICAL; setPadding(ui.dp(4), 0, ui.dp(8), 0) }
            texts.addView(ui.text(it.title, 16f, bold = true))
            texts.addView(ui.text(it.why, 13f, ui.onSurfaceVariant))
            if (it.hint != null && it.ok != true) texts.addView(ui.text(it.hint, 13f, ui.primary), ui.lp(top = 4))
            row.addView(texts, LinearLayout.LayoutParams(0, -2, 1f))
            if (it.ok != true) row.addView(ui.button(it.action, filled = it.required) { onFix() }.apply {
                textSize = 14f; setPadding(ui.dp(14), ui.dp(8), ui.dp(14), ui.dp(8))
            })
            return row
        }
    }
}

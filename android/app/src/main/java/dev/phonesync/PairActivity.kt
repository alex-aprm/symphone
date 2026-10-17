package dev.phonesync

import android.app.Activity
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.view.Gravity
import android.view.View
import android.widget.LinearLayout
import android.widget.ProgressBar
import android.widget.ScrollView
import android.widget.TextView

/**
 * Runs QR pairing for a `phonesync://pair?...` link: from the in-app scanner (starts right away) or from the system
 * camera / Lens / any other app (asks for confirmation first, since the link decides where our token is sent).
 */
class PairActivity : Activity() {
    private lateinit var ui: Ui
    private lateinit var title: TextView
    private lateinit var status: TextView
    private lateinit var progress: ProgressBar
    private lateinit var buttons: LinearLayout
    @Volatile private var running = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        requestWindowFeature(android.view.Window.FEATURE_NO_TITLE)
        Hub.startService(this)
        ui = Ui(this)
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(ui.dp(24), ui.dp(32), ui.dp(24), ui.dp(24))
        }
        title = ui.text("Pair with a computer", 26f, bold = true)
        root.addView(title, ui.lp(bottom = 16))
        val card = ui.card()
        progress = ProgressBar(this).apply { isIndeterminate = true; indeterminateTintList = android.content.res.ColorStateList.valueOf(ui.primary) }
        card.addView(progress, LinearLayout.LayoutParams(ui.dp(48), ui.dp(48)).apply { gravity = Gravity.CENTER_HORIZONTAL; bottomMargin = ui.dp(12) })
        status = ui.text("", 16f, ui.onSurfaceVariant).apply { gravity = Gravity.CENTER_HORIZONTAL }
        card.addView(status, ui.lp())
        root.addView(card, ui.lp(bottom = 16))
        buttons = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        root.addView(buttons, ui.lp())
        val scroll = ScrollView(this).apply { addView(root); setBackgroundColor(ui.surface) }
        setContentView(scroll)
        ui.applyWindow(this, root)
        handle(intent)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        if (!running) handle(intent)
    }

    private fun handle(intent: Intent?) {
        val uri = intent?.data
        val req = Pairing.parse(uri)
        if (req == null) {
            showResult(false, "This is not a valid Symphone pairing code.")
            return
        }
        val trusted = uri != null && uri == trustedUri
        trustedUri = null
        if (trusted) start(req) else confirm(req)
    }

    private fun confirm(req: Pairing.Request) {
        progress.visibility = View.GONE
        title.text = "Pair with ${req.pcName}?"
        status.text = buildString {
            append("${req.pcName} will be able to access this phone's files, notifications and media.")
            if (req.addrs.isNotEmpty()) append("\n\nAddress: ${req.addrs.joinToString(", ")}")
        }
        buttons.removeAllViews()
        buttons.addView(ui.button("Pair") { start(req) }, ui.lp(bottom = 8))
        buttons.addView(ui.button("Cancel", filled = false) { finish() }, ui.lp())
    }

    private fun start(req: Pairing.Request) {
        running = true
        title.text = "Pairing with ${req.pcName}"
        progress.visibility = View.VISIBLE
        status.text = "Starting…"
        buttons.removeAllViews()
        buttons.addView(ui.button("Cancel", filled = false) { Ble.cancelPairing(); finish() }, ui.lp())
        Thread({
            val pc = try { Pairing.run(req) { msg -> runOnUiThread { status.text = msg } } } catch (e: Exception) { null }
            runOnUiThread {
                running = false
                if (isFinishing || isDestroyed) return@runOnUiThread
                if (pc != null) showResult(true, "Paired with ${pc.name}" + (if (pc.master) " (master)." else "."))
                else showResult(false, "Couldn't reach ${req.pcName}. Make sure the PC is on and on the same network, then try again.")
            }
        }, "pairing").start()
    }

    private fun showResult(ok: Boolean, msg: String) {
        progress.visibility = View.GONE
        title.text = if (ok) "Paired" else "Pairing failed"
        status.text = msg
        status.setTextColor(if (ok) ui.onSurface else ui.bad)
        buttons.removeAllViews()
        buttons.addView(ui.button("Done") {
            startActivity(Intent(this, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP))
            finish()
        }, ui.lp())
        if (ok) window.decorView.performHapticFeedback(android.view.HapticFeedbackConstants.CONFIRM)
        MainActivity.refresh()
    }

    override fun onDestroy() {
        if (running) Ble.cancelPairing()
        super.onDestroy()
    }

    companion object {
        /** Set by the in-app scanner right before starting us: such a link needs no confirmation. */
        @Volatile var trustedUri: Uri? = null
    }
}

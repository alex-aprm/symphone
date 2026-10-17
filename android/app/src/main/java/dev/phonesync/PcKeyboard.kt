package dev.phonesync

import android.inputmethodservice.InputMethodService
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.provider.Settings
import android.view.KeyCharacterMap
import android.view.KeyEvent
import android.view.View
import android.view.inputmethod.EditorInfo
import android.view.inputmethod.InputMethodManager
import org.json.JSONObject
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/**
 * Invisible input method used while a Symphone PC window is being typed into: the PC keyboard's text (any script)
 * is committed through this IME's InputConnection, and nothing is drawn on any screen.
 *
 * The PC switches to it (`ime enable/set dev.phonesync/.PcKeyboard`) and keeps a lease alive. Without a valid lease,
 * or for an editor of an app that isn't in a PC window, it hands the keyboard straight back to the user's own IME.
 */
class PcKeyboard : InputMethodService() {

    private val handler = Handler(Looper.getMainLooper())
    private val watchdog = object : Runnable {
        override fun run() {
            if (instance !== this@PcKeyboard) return
            if (Lease.current == null) { switchBack("lease expired"); return }
            handler.postDelayed(this, 5000)
        }
    }

    override fun onCreate() {
        super.onCreate()
        instance = this
        handler.postDelayed(watchdog, 5000)
    }

    override fun onDestroy() {
        if (instance === this) instance = null
        handler.removeCallbacksAndMessages(null)
        super.onDestroy()
    }

    // ---- never visible
    override fun onEvaluateInputViewShown() = false
    override fun onEvaluateFullscreenMode() = false
    override fun onShowInputRequested(flags: Int, configChange: Boolean) = false
    override fun onCreateInputView(): View = View(this).apply { minimumHeight = 0 }
    override fun onCreateCandidatesView(): View? = null

    override fun onStartInput(attribute: EditorInfo?, restarting: Boolean) {
        super.onStartInput(attribute, restarting)
        editorPkg = attribute?.packageName
        editorType = attribute?.inputType ?: 0
        val pkg = editorPkg
        handler.post { recheck(pkg) }
        handler.removeCallbacks(watchdog)
        handler.postDelayed(watchdog, 5000)
    }

    override fun onFinishInput() {
        super.onFinishInput()
        editorPkg = null
        editorType = 0
    }

    /** Display the keyboard is bound to: the editor's display when PC displays use the "local" IME policy. */
    fun imeDisplay(): Int = try {
        if (android.os.Build.VERSION.SDK_INT >= 30) display?.displayId ?: 0 else 0
    } catch (_: Exception) { 0 }

    /** Hands the keyboard back unless the field is on a PC window's display (or a PC lease covers its app). */
    fun recheck(pkg: String? = editorPkg) {
        val l = Lease.current
        val d = imeDisplay()
        when {
            l == null -> switchBack("no lease")
            d != 0 -> Unit                          // a field in a PC window: keep typing from the PC
            l.any || (pkg != null && pkg in l.pkgs) && !phoneInUse() -> Unit
            else -> switchBack("$pkg on the phone's own screen")
        }
    }

    /** The phone's own screen is on and unlocked: someone may be typing on it. */
    private fun phoneInUse(): Boolean = try {
        val pm = getSystemService(android.os.PowerManager::class.java)
        val km = getSystemService(android.app.KeyguardManager::class.java)
        pm.isInteractive && !km.isKeyguardLocked
    } catch (_: Exception) { true }

    /** Back to the user's keyboard; never leaves the phone without one. */
    fun switchBack(why: String) {
        if (instance !== this) return
        android.util.Log.i("Symphone", "PC keyboard: switching back ($why)")
        val prev = Lease.prev
        try {
            if (!prev.isNullOrEmpty() && prev != ID && isEnabled(prev)) {
                switchInputMethod(prev)
                return
            }
        } catch (e: Exception) {
            android.util.Log.w("Symphone", "switchInputMethod($prev): $e")
        }
        try {
            if (switchToPreviousInputMethod()) return
        } catch (e: Exception) {
            android.util.Log.w("Symphone", "switchToPreviousInputMethod: $e")
        }
        try {   // last resort: any other enabled IME
            val imm = getSystemService(InputMethodManager::class.java)
            val other = imm.enabledInputMethodList.firstOrNull { it.id != ID }
            if (other != null) switchInputMethod(other.id)
        } catch (e: Exception) {
            android.util.Log.w("Symphone", "switch to any IME: $e")
        }
    }

    private fun isEnabled(id: String): Boolean = try {
        getSystemService(InputMethodManager::class.java).enabledInputMethodList.any { it.id == id }
    } catch (_: Exception) { false }

    /** Runs on the main thread. Returns null on success or a reason. */
    private fun input(b: JSONObject): String? {
        val ic = currentInputConnection ?: return "no-editor"
        // Windows without a text field still hand IMEs a dummy connection (inputType TYPE_NULL): typing there must
        // fall back to key injection on the PC side.
        if (editorPkg == null || editorType == EditorInfo.TYPE_NULL) return "no-editor"
        when {
            b.has("text") -> ic.commitText(b.optString("text"), 1)
            b.has("paste") -> ic.commitText(b.optString("paste"), 1)
            b.has("key") -> {
                val code = b.optInt("key")
                val meta = b.optInt("meta", 0)
                val repeat = b.optInt("repeat", 0)
                val now = SystemClock.uptimeMillis()
                fun ev(action: Int) = KeyEvent(now, now, action, code, repeat, meta, KeyCharacterMap.VIRTUAL_KEYBOARD, 0,
                    KeyEvent.FLAG_SOFT_KEYBOARD or KeyEvent.FLAG_KEEP_TOUCH_MODE)
                when (b.optString("action", "tap")) {
                    "down" -> ic.sendKeyEvent(ev(KeyEvent.ACTION_DOWN))
                    "up" -> ic.sendKeyEvent(ev(KeyEvent.ACTION_UP))
                    else -> { ic.sendKeyEvent(ev(KeyEvent.ACTION_DOWN)); ic.sendKeyEvent(ev(KeyEvent.ACTION_UP)) }
                }
            }
            else -> return "bad-request"
        }
        return null
    }

    class LeaseData(val pkgs: Set<String>, val any: Boolean, val until: Long)

    object Lease {
        @Volatile private var data: LeaseData? = null
        @Volatile var prev: String? = null

        val current: LeaseData? get() = data?.takeIf { SystemClock.elapsedRealtime() < it.until }

        fun set(pkgs: Set<String>, any: Boolean, prev: String?, ttlSec: Int) {
            data = LeaseData(pkgs, any, SystemClock.elapsedRealtime() + ttlSec.coerceIn(1, 3600) * 1000L)
            if (!prev.isNullOrEmpty() && prev != ID) this.prev = prev
            main.post { instance?.recheck() }        // the PC may have just taken this app off its windows
        }

        fun clear() { data = null }
    }

    companion object {
        const val ID = "dev.phonesync/.PcKeyboard"
        @Volatile var instance: PcKeyboard? = null
        @Volatile var editorPkg: String? = null
        @Volatile var editorType: Int = 0
        private val main = Handler(Looper.getMainLooper())

        fun currentIme(): String? = try {
            Settings.Secure.getString(Hub.ctx.contentResolver, Settings.Secure.DEFAULT_INPUT_METHOD)
        } catch (_: Exception) { null }

        fun isCurrent(): Boolean = currentIme() == ID || currentIme() == "dev.phonesync/dev.phonesync.PcKeyboard"

        fun status(): JSONObject {
            val kb = instance
            return JSONObject().put("ok", true).put("current", currentIme() ?: JSONObject.NULL)
                .put("ours", isCurrent())
                .put("editor", editorPkg ?: JSONObject.NULL)
                .put("display", kb?.imeDisplay() ?: JSONObject.NULL)
                .put("bound", kb?.currentInputConnection != null && editorPkg != null && editorType != EditorInfo.TYPE_NULL)
        }

        fun release() {
            Lease.clear()
            main.post { if (isCurrent()) instance?.switchBack("released by PC") }
        }

        /** Synchronous on the main thread (bounded), so HTTP request order == input order. */
        fun input(b: JSONObject): JSONObject {
            val kb = instance
            if (kb == null || !isCurrent()) return JSONObject().put("ok", false).put("reason", "not-current")
            var err: String? = "timeout"
            val latch = CountDownLatch(1)
            main.post {
                err = try { kb.input(b) } catch (e: Exception) { "error: $e" }
                latch.countDown()
            }
            latch.await(500, TimeUnit.MILLISECONDS)
            val e = err
            return if (e == null) JSONObject().put("ok", true) else JSONObject().put("ok", false).put("reason", e)
        }
    }
}

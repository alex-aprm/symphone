package dev.phonesync

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.app.KeyguardManager
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.graphics.Path
import android.graphics.Rect
import android.media.AudioManager
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.PowerManager
import android.os.SystemClock
import android.provider.Settings
import android.util.Log
import android.view.KeyEvent
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import org.json.JSONObject
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/**
 * "Symphone control" accessibility service: the debug-free way for the PC to drive the phone. Touch from the PC
 * becomes gestures (continued strokes, several fingers), keys become global actions or edits of the focused text
 * field, and it can lock the phone and type the PIN into the keyguard (once, never retried).
 *
 * Coordinates are normalized (0..1) to the phone's own (default) display, which is what screen casting shows.
 */
class ControlService : AccessibilityService() {
    override fun onServiceConnected() {
        instance = this
        Log.i(TAG, "control service on")
    }

    override fun onDestroy() {
        if (instance === this) instance = null
        super.onDestroy()
    }

    override fun onUnbind(intent: Intent?): Boolean {
        if (instance === this) instance = null
        return super.onUnbind(intent)
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {
        if (event != null && SystemClock.uptimeMillis() < consentUntil) main.post { confirmConsent() }
    }

    // ------------------------------------------------------------------ screen-casting consent
    /**
     * When a paired PC opens the phone's screen and Symphone itself just asked to cast ([expectConsent]), presses
     * Start in Android's "start recording?" dialog, so it works without anyone at the phone. Only that dialog, only
     * while it names Symphone, only within a few seconds of our own request. (With the PROJECT_MEDIA app-op, set by
     * the PC whenever it has adb, Android doesn't ask at all.)
     */
    private fun confirmConsent() {
        if (SystemClock.uptimeMillis() >= consentUntil) return
        val label = applicationInfo.loadLabel(packageManager).toString()
        for (w in windows) {
            val root = w.root ?: continue
            val pkg = root.packageName?.toString() ?: ""
            if (pkg == packageName || !(pkg.contains("systemui") || pkg == "android")) continue
            if (root.findAccessibilityNodeInfosByText(label).isEmpty()) continue    // not our request
            val button = root.findAccessibilityNodeInfosByViewId("android:id/button1").firstOrNull { it.isEnabled }
                ?: findStart(root)
            if (button != null && button.performAction(AccessibilityNodeInfo.ACTION_CLICK)) {
                Log.i(TAG, "screen casting allowed for the PC")
                consentUntil = 0
                return
            }
        }
        main.postDelayed({ confirmConsent() }, 250)           // the dialog may still be building
    }

    private fun findStart(n: AccessibilityNodeInfo): AccessibilityNodeInfo? {
        val t = (n.text ?: n.contentDescription)?.toString()?.trim()?.lowercase() ?: ""
        if (n.isClickable && n.isEnabled && START.any { t == it || t.startsWith("$it ") }) return n
        for (i in 0 until n.childCount) n.getChild(i)?.let { c -> findStart(c)?.let { return it } }
        return null
    }
    override fun onInterrupt() {}

    // ------------------------------------------------------------------ gestures
    private val main = Handler(Looper.getMainLooper())

    /** One finger of the gesture being built: where it is, and the stroke that ends there. */
    private class Finger(var x: Float, var y: Float) {
        var stroke: GestureDescription.StrokeDescription? = null
        var pendingPath: Path? = null          // movement not dispatched yet
        var px = x; var py = y                 // end of pendingPath
        var up = false
        var started = false
    }

    private val fingers = LinkedHashMap<Int, Finger>()
    private var inFlight = false
    private var lastDispatch = 0L

    private fun screen(): Rect {
        val wm = getSystemService(android.view.WindowManager::class.java)
        return if (Build.VERSION.SDK_INT >= 30) wm.maximumWindowMetrics.bounds
        else Rect(0, 0, resources.displayMetrics.widthPixels, resources.displayMetrics.heightPixels)
    }

    /** a: MotionEvent action (0 down, 1 up, 2 move); x, y normalized. */
    fun touch(a: Int, id: Int, xn: Double, yn: Double) = main.post {
        val r = screen()
        val x = (xn.coerceIn(0.0, 1.0) * (r.width() - 1)).toFloat()
        val y = (yn.coerceIn(0.0, 1.0) * (r.height() - 1)).toFloat()
        when (a) {
            0 -> fingers[id] = Finger(x, y)
            2, 1 -> {
                val f = fingers[id] ?: return@post
                val p = f.pendingPath ?: Path().also { it.moveTo(f.px, f.py); f.pendingPath = it }
                if (x != f.px || y != f.py) p.lineTo(x, y)
                f.px = x; f.py = y
                if (a == 1) f.up = true
            }
        }
        pump()
    }

    /** Sends everything that changed since the last dispatch as one gesture (all fingers together). */
    private fun pump() {
        if (inFlight || fingers.isEmpty()) return
        val now = SystemClock.uptimeMillis()
        val dur = (now - lastDispatch).coerceIn(1, 40)
        val b = GestureDescription.Builder()
        var any = false
        for ((_, f) in fingers) {
            // a finger holding still keeps its stroke alive with a tiny continuation (strokes can't be empty)
            val path = f.pendingPath ?: Path().also {
                it.moveTo(f.px, f.py); f.px += if (f.px > 1f) -0.5f else 0.5f; it.lineTo(f.px, f.py)
            }
            val s = if (!f.started) {
                GestureDescription.StrokeDescription(Path().apply { moveTo(f.x, f.y); lineTo(f.px, f.py) }, 0, dur, !f.up)
            } else {
                f.stroke!!.continueStroke(path, 0, dur, !f.up)
            }
            f.stroke = s; f.started = true; f.pendingPath = null
            b.addStroke(s)
            any = true
        }
        if (!any) return
        inFlight = true
        lastDispatch = now
        val ok = dispatchGesture(b.build(), object : GestureResultCallback() {
            override fun onCompleted(g: GestureDescription?) = done()
            override fun onCancelled(g: GestureDescription?) = done()
        }, main)
        if (!ok) done()
        fingers.entries.removeAll { it.value.up }
    }

    private fun done() {
        inFlight = false
        // keep held fingers alive and send queued movement
        if (fingers.values.any { it.pendingPath != null || it.up } ) pump()
        else if (fingers.isNotEmpty()) main.postDelayed({ if (!inFlight && fingers.isNotEmpty()) pump() }, 30)
    }

    /** Mouse wheel: a short swipe in the scroll direction (v > 0 scrolls up = content moves down). */
    fun scroll(xn: Double, yn: Double, h: Double, v: Double) = main.post {
        if (fingers.isNotEmpty()) return@post
        val r = screen()
        val x = (xn * r.width()).toFloat(); val y = (yn * r.height()).toFloat()
        val dy = (v * r.height() * 0.12).toFloat(); val dx = (h * r.width() * 0.12).toFloat()
        val p = Path().apply {
            moveTo(x, y)
            lineTo((x + dx).coerceIn(1f, r.width() - 2f), (y + dy).coerceIn(1f, r.height() - 2f))
        }
        dispatchGesture(GestureDescription.Builder().addStroke(GestureDescription.StrokeDescription(p, 0, 120)).build(), null, main)
    }

    fun swipe(x0: Float, y0: Float, x1: Float, y1: Float, ms: Long) {
        val p = Path().apply { moveTo(x0, y0); lineTo(x1, y1) }
        main.post { dispatchGesture(GestureDescription.Builder().addStroke(GestureDescription.StrokeDescription(p, 0, ms)).build(), null, main) }
    }

    // ------------------------------------------------------------------ keys and text
    fun global(action: String): Boolean {
        val g = when (action) {
            "back" -> GLOBAL_ACTION_BACK
            "home" -> GLOBAL_ACTION_HOME
            "recents" -> GLOBAL_ACTION_RECENTS
            "notifications" -> GLOBAL_ACTION_NOTIFICATIONS
            "quick_settings" -> GLOBAL_ACTION_QUICK_SETTINGS
            "lock" -> if (Build.VERSION.SDK_INT >= 28) GLOBAL_ACTION_LOCK_SCREEN else return false
            else -> return false
        }
        return performGlobalAction(g)
    }

    /** Android keycode, down (a=0) only; up events are ignored (the PC sends both). */
    fun key(code: Int, a: Int, meta: Int) = main.post {
        if (a == 1) return@post
        val ctrl = meta and KeyEvent.META_CTRL_ON != 0
        when (code) {
            KeyEvent.KEYCODE_BACK, KeyEvent.KEYCODE_ESCAPE -> global("back")
            KeyEvent.KEYCODE_HOME -> global("home")
            KeyEvent.KEYCODE_APP_SWITCH -> global("recents")
            KeyEvent.KEYCODE_NOTIFICATION -> global("notifications")
            KeyEvent.KEYCODE_POWER -> global("lock")
            KeyEvent.KEYCODE_VOLUME_UP, KeyEvent.KEYCODE_VOLUME_DOWN, KeyEvent.KEYCODE_VOLUME_MUTE ->
                getSystemService(AudioManager::class.java).adjustStreamVolume(AudioManager.STREAM_MUSIC,
                    when (code) { KeyEvent.KEYCODE_VOLUME_UP -> AudioManager.ADJUST_RAISE
                        KeyEvent.KEYCODE_VOLUME_DOWN -> AudioManager.ADJUST_LOWER else -> AudioManager.ADJUST_TOGGLE_MUTE },
                    AudioManager.FLAG_SHOW_UI)
            else -> edit(code, ctrl, meta and KeyEvent.META_SHIFT_ON != 0)
        }
    }

    private fun focused(): AccessibilityNodeInfo? =
        findFocus(AccessibilityNodeInfo.FOCUS_INPUT)?.takeIf { it.isEditable }

    /** Current text without the hint, and the selection (clamped). */
    private fun state(n: AccessibilityNodeInfo): Triple<String, Int, Int> {
        val t = if (Build.VERSION.SDK_INT >= 26 && n.isShowingHintText) "" else (n.text?.toString() ?: "")
        var s = n.textSelectionStart; var e = n.textSelectionEnd
        if (s < 0 || e < 0) { s = t.length; e = t.length }
        s = s.coerceIn(0, t.length); e = e.coerceIn(0, t.length)
        return Triple(t, minOf(s, e), maxOf(s, e))
    }

    private fun setText(n: AccessibilityNodeInfo, text: String, cursor: Int) {
        n.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, Bundle().apply {
            putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, text)
        })
        select(n, cursor, cursor)
    }

    private fun select(n: AccessibilityNodeInfo, s: Int, e: Int) {
        n.performAction(AccessibilityNodeInfo.ACTION_SET_SELECTION, Bundle().apply {
            putInt(AccessibilityNodeInfo.ACTION_ARGUMENT_SELECTION_START_INT, s)
            putInt(AccessibilityNodeInfo.ACTION_ARGUMENT_SELECTION_END_INT, e)
        })
    }

    /** Text from the PC, inserted at the cursor of the focused field (replacing a selection). */
    fun text(s: String): Boolean {
        var ok = false
        val l = CountDownLatch(1)
        main.post {
            try {
                val n = focused()
                if (n != null) {
                    val (t, a, b) = state(n)
                    setText(n, t.substring(0, a) + s + t.substring(b), a + s.length)
                    ok = true
                }
            } catch (e: Exception) { Log.w(TAG, "text: $e") }
            l.countDown()
        }
        l.await(500, TimeUnit.MILLISECONDS)
        return ok
    }

    private fun edit(code: Int, ctrl: Boolean, shift: Boolean) {
        val n = focused() ?: return
        val (t, a, b) = state(n)
        if (ctrl) {
            when (code) {
                KeyEvent.KEYCODE_A -> select(n, 0, t.length)
                KeyEvent.KEYCODE_C -> n.performAction(AccessibilityNodeInfo.ACTION_COPY)
                KeyEvent.KEYCODE_X -> n.performAction(AccessibilityNodeInfo.ACTION_CUT)
                KeyEvent.KEYCODE_V -> n.performAction(AccessibilityNodeInfo.ACTION_PASTE)
            }
            return
        }
        when (code) {
            KeyEvent.KEYCODE_DEL -> if (a != b) setText(n, t.removeRange(a, b), a)
                else if (a > 0) setText(n, t.removeRange(a - 1, a), a - 1)
            KeyEvent.KEYCODE_FORWARD_DEL -> if (a != b) setText(n, t.removeRange(a, b), a)
                else if (a < t.length) setText(n, t.removeRange(a, a + 1), a)
            KeyEvent.KEYCODE_ENTER, KeyEvent.KEYCODE_NUMPAD_ENTER -> {
                val multi = n.isMultiLine
                if (!multi && Build.VERSION.SDK_INT >= 30 &&
                    n.performAction(AccessibilityNodeInfo.AccessibilityAction.ACTION_IME_ENTER.id)) return
                setText(n, t.substring(0, a) + "\n" + t.substring(b), a + 1)
            }
            KeyEvent.KEYCODE_TAB -> setText(n, t.substring(0, a) + "\t" + t.substring(b), a + 1)
            KeyEvent.KEYCODE_DPAD_LEFT -> {
                val p = if (a != b) a else maxOf(0, a - 1)
                if (shift) select(n, maxOf(0, a - 1), b) else select(n, p, p)
            }
            KeyEvent.KEYCODE_DPAD_RIGHT -> {
                val p = minOf(t.length, if (a != b) b else b + 1)
                if (shift) select(n, a, p) else select(n, p, p)
            }
            KeyEvent.KEYCODE_MOVE_HOME -> if (shift) select(n, 0, b) else select(n, 0, 0)
            KeyEvent.KEYCODE_MOVE_END -> if (shift) select(n, a, t.length) else select(n, t.length, t.length)
            KeyEvent.KEYCODE_SPACE -> setText(n, t.substring(0, a) + " " + t.substring(b), a + 1)
        }
    }

    // ------------------------------------------------------------------ unlock
    /**
     * Wakes the screen, brings up the bouncer and taps the PIN digits found in the keyguard's window, then OK/Enter.
     * One attempt only (wrong PINs count towards Android's lockout). Blocking: call off the main thread.
     */
    fun unlock(pin: String): JSONObject {
        val km = getSystemService(KeyguardManager::class.java)
        val pm = getSystemService(PowerManager::class.java)
        @Suppress("DEPRECATION")
        val wl = pm.newWakeLock(PowerManager.SCREEN_BRIGHT_WAKE_LOCK or PowerManager.ACQUIRE_CAUSES_WAKEUP, "phonesync:unlock")
        wl.acquire(10_000)
        try { return unlockAwake(pin, km, pm) } finally { if (wl.isHeld) wl.release() }
    }

    private fun unlockAwake(pin: String, km: KeyguardManager, pm: PowerManager): JSONObject {
        if (!pm.isInteractive) {
            for (i in 0 until 20) { if (pm.isInteractive) break; Thread.sleep(100) }
            Thread.sleep(500)   // gestures during the screen-on animation get dropped
        }
        if (!km.isKeyguardLocked) return JSONObject().put("ok", true).put("message", "already unlocked")
        // Tap the lock icon (SystemUI ignores synthetic swipes on the lock screen on Pixels), else swipe up
        val r = screen()
        val open = { iconFirst: Boolean ->
            var clicked = false
            if (iconFirst) {
                val l = CountDownLatch(1)
                main.post { try { clicked = findLockIcon()?.performAction(AccessibilityNodeInfo.ACTION_CLICK) == true } finally { l.countDown() } }
                l.await(500, TimeUnit.MILLISECONDS)
            }
            if (!clicked) swipe(r.width() / 2f, r.height() * 0.85f, r.width() / 2f, r.height() * 0.25f, 250)
        }
        if (!km.isDeviceSecure) {
            for (attempt in 0 until 2) {
                open(attempt == 0)
                for (i in 0 until 10) { if (!km.isKeyguardLocked) break; Thread.sleep(150) }
                if (!km.isKeyguardLocked) break
            }
            return JSONObject().put("ok", !km.isKeyguardLocked).put("message", "swiped")
        }
        // Bouncer (bringing up the keypad again is harmless, typing the PIN is not)
        var digits = findDigits()
        for (attempt in 0 until 2) {
            if (pin.all { it in digits }) break
            open(attempt == 0)
            for (i in 0 until 14) { Thread.sleep(150); digits = findDigits(); if (pin.all { it in digits }) break }
        }
        if (pin.any { it !in digits }) {
            return JSONObject().put("ok", false).put("error", "couldn't find the PIN keypad on the lock screen")
        }
        Thread.sleep(600)   // let the keypad finish appearing
        digits = findDigits().ifEmpty { digits }
        for (c in pin) {
            val key = findDigits()[c] ?: digits[c]   // findDigits() waits for the main thread, so not inside post
            val l = CountDownLatch(1)
            var ok = false
            main.post { try { ok = key?.performAction(AccessibilityNodeInfo.ACTION_CLICK) == true } finally { l.countDown() } }
            l.await(500, TimeUnit.MILLISECONDS)
            // the keypad went away mid-PIN: stop before Enter so a partial PIN isn't counted as a wrong attempt
            if (!ok) return JSONObject().put("ok", false).put("error", "the lock screen keypad closed while typing — not retrying")
            Thread.sleep(90)
        }
        Thread.sleep(150)
        if (km.isKeyguardLocked) {
            val l = CountDownLatch(1)
            main.post { try { findEnter()?.performAction(AccessibilityNodeInfo.ACTION_CLICK) } finally { l.countDown() } }
            l.await(500, TimeUnit.MILLISECONDS)
        }
        for (i in 0 until 12) {
            if (!km.isKeyguardLocked) return JSONObject().put("ok", true).put("message", "unlocked")
            Thread.sleep(150)
        }
        return JSONObject().put("ok", false).put("error", "the phone is still locked (wrong PIN?) — not retrying")
    }

    /** Digit buttons of the keyguard's PIN pad: '0'..'9' -> node. */
    private fun findDigits(): Map<Char, AccessibilityNodeInfo> {
        val out = HashMap<Char, AccessibilityNodeInfo>()
        val l = CountDownLatch(1)
        main.post {
            try {
                for (w in windows) {
                    val root = w.root ?: continue
                    walk(root) { n ->
                        val id = n.viewIdResourceName ?: ""
                        val m = Regex("""key(\d)$""").find(id)
                        val label = (n.text ?: n.contentDescription)?.toString()?.trim() ?: ""
                        val d = m?.groupValues?.get(1)?.single() ?: label.takeIf { it.length == 1 && it[0].isDigit() }?.single()
                            ?: label.takeIf { it.isNotEmpty() && it[0].isDigit() && n.isClickable && (id.contains("key") || id.contains("digit")) }?.get(0)
                        if (d != null && (n.isClickable || id.isNotEmpty()) && d !in out) out[d] = n
                    }
                }
            } catch (e: Exception) { Log.w(TAG, "digits: $e") }
            l.countDown()
        }
        l.await(800, TimeUnit.MILLISECONDS)
        return out
    }

    /** The lock screen's lock icon, which opens the PIN pad when clicked (main thread). */
    private fun findLockIcon(): AccessibilityNodeInfo? {
        var hit: AccessibilityNodeInfo? = null
        for (w in windows) {
            val root = w.root ?: continue
            walk(root) { n ->
                val id = n.viewIdResourceName ?: ""
                if (hit == null && n.isClickable && (id.endsWith("device_entry_icon_view") || id.endsWith("lock_icon_view") || id.endsWith("lock_icon")))
                    hit = n
            }
        }
        return hit
    }

    private fun findEnter(): AccessibilityNodeInfo? {
        var hit: AccessibilityNodeInfo? = null
        for (w in windows) {
            val root = w.root ?: continue
            walk(root) { n ->
                val id = n.viewIdResourceName ?: ""
                val label = (n.contentDescription ?: n.text)?.toString()?.lowercase() ?: ""
                if (hit == null && (id.endsWith("key_enter") || id.endsWith("key_ok") || label == "ok" || label == "enter" || label == "done"))
                    hit = n
            }
        }
        return hit
    }

    private fun walk(n: AccessibilityNodeInfo, depth: Int = 0, f: (AccessibilityNodeInfo) -> Unit) {
        if (depth > 40) return
        f(n)
        for (i in 0 until n.childCount) n.getChild(i)?.let { walk(it, depth + 1, f) }
    }

    companion object {
        const val TAG = "SymphoneCtl"
        @Volatile var instance: ControlService? = null
        @Volatile private var consentUntil = 0L
        /** The consent dialog's confirm button, when it has no standard id (a few languages; ids are tried first). */
        private val START = listOf("start now", "start", "share screen", "share", "начать", "начать сейчас",
            "iniciar", "iniciar agora", "começar", "démarrer", "starten", "jetzt starten", "avvia", "开始", "立即开始")

        /** Symphone is about to ask Android for screen casting: confirm that dialog for the next few seconds. */
        fun expectConsent() {
            val s = instance ?: return
            consentUntil = SystemClock.uptimeMillis() + 15_000
            s.main.postDelayed({ s.confirmConsent() }, 300)
        }

        fun enabled(ctx: Context): Boolean {
            val me = ComponentName(ctx, ControlService::class.java).flattenToString()
            val list = Settings.Secure.getString(ctx.contentResolver, Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES) ?: ""
            return list.split(':').any { it.equals(me, true) || it.equals("${ctx.packageName}/.ControlService", true) }
        }

        /** One input message from the PC (stream socket or POST /api/input). Returns false if it can't act. */
        fun input(m: JSONObject): Boolean {
            val s = instance ?: return false
            when (m.optString("t")) {
                "touch" -> s.touch(m.optInt("a"), m.optInt("id"), m.optDouble("x"), m.optDouble("y"))
                "scroll" -> s.scroll(m.optDouble("x", .5), m.optDouble("y", .5), m.optDouble("h"), m.optDouble("v"))
                "key" -> s.key(m.optInt("code"), m.optInt("a", 0), m.optInt("meta"))
                "text" -> return s.text(m.optString("s"))
                "global" -> return s.global(m.optString("action"))
                else -> return false
            }
            return true
        }
    }
}

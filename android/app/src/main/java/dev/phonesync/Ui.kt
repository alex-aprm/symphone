package dev.phonesync

import android.app.Activity
import android.content.Context
import android.content.res.ColorStateList
import android.content.res.Configuration
import android.graphics.Color
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.graphics.drawable.RippleDrawable
import android.os.Build
import android.view.Gravity
import android.view.View
import android.view.WindowInsets
import android.widget.LinearLayout
import android.widget.TextView

/** Tiny Material You-ish palette + view helpers on platform APIs only (dynamic colors on API 31+). */
@android.annotation.SuppressLint("NewApi", "InlinedApi")
class Ui(val ctx: Context) {
    val dark = (ctx.resources.configuration.uiMode and Configuration.UI_MODE_NIGHT_MASK) == Configuration.UI_MODE_NIGHT_YES
    private val dyn = Build.VERSION.SDK_INT >= 31
    private val d = ctx.resources.displayMetrics.density

    private fun sys(id: Int, fallback: Int): Int = if (dyn) try { ctx.getColor(id) } catch (_: Exception) { fallback } else fallback

    // Roles roughly following Material 3 tonal mapping.
    val primary = if (dark) sys(android.R.color.system_accent1_200, 0xFFA8C7FA.toInt()) else sys(android.R.color.system_accent1_600, 0xFF0B57D0.toInt())
    val onPrimary = if (dark) sys(android.R.color.system_accent1_800, 0xFF062E6F.toInt()) else sys(android.R.color.system_accent1_0, Color.WHITE)
    val primaryContainer = if (dark) sys(android.R.color.system_accent1_700, 0xFF0842A0.toInt()) else sys(android.R.color.system_accent1_100, 0xFFD3E3FD.toInt())
    val onPrimaryContainer = if (dark) sys(android.R.color.system_accent1_100, 0xFFD3E3FD.toInt()) else sys(android.R.color.system_accent1_900, 0xFF041E49.toInt())
    val secondaryContainer = if (dark) sys(android.R.color.system_accent2_700, 0xFF3F4759.toInt()) else sys(android.R.color.system_accent2_100, 0xFFDBE2F9.toInt())
    val surface = if (dark) sys(android.R.color.system_neutral1_900, 0xFF111318.toInt()) else sys(android.R.color.system_neutral1_10, 0xFFF9F9FF.toInt())
    val surfaceContainer = if (dark) sys(android.R.color.system_neutral1_800, 0xFF1E2025.toInt()) else sys(android.R.color.system_neutral2_50, 0xFFEDEEF4.toInt())
    val onSurface = if (dark) sys(android.R.color.system_neutral1_100, 0xFFE2E2E9.toInt()) else sys(android.R.color.system_neutral1_900, 0xFF1A1B20.toInt())
    val onSurfaceVariant = if (dark) sys(android.R.color.system_neutral2_200, 0xFFC4C6D0.toInt()) else sys(android.R.color.system_neutral2_700, 0xFF44474F.toInt())
    val outline = if (dark) sys(android.R.color.system_neutral2_500, 0xFF8E9099.toInt()) else sys(android.R.color.system_neutral2_400, 0xFF74777F.toInt())
    val good = if (dark) 0xFF6DD58C.toInt() else 0xFF1E8E3E.toInt()
    val bad = if (dark) 0xFFFFB4AB.toInt() else 0xFFBA1A1A.toInt()

    fun dp(v: Int) = (v * d).toInt()
    fun dp(v: Float) = v * d

    fun rounded(color: Int, radiusDp: Int, strokeColor: Int? = null) = GradientDrawable().apply {
        setColor(color); cornerRadius = dp(radiusDp.toFloat())
        if (strokeColor != null) setStroke(dp(1), strokeColor)
    }

    fun ripple(bg: GradientDrawable?, radiusDp: Int): RippleDrawable {
        val mask = rounded(Color.WHITE, radiusDp)
        return RippleDrawable(ColorStateList.valueOf((onSurface and 0x00FFFFFF) or 0x22000000), bg, mask)
    }

    fun text(s: CharSequence, sizeSp: Float, color: Int = onSurface, bold: Boolean = false) = TextView(ctx).apply {
        text = s; textSize = sizeSp; setTextColor(color)
        if (bold) typeface = Typeface.create("sans-serif-medium", Typeface.NORMAL)
    }

    fun card(): LinearLayout = LinearLayout(ctx).apply {
        orientation = LinearLayout.VERTICAL
        background = rounded(surfaceContainer, 24)
        setPadding(dp(20), dp(16), dp(20), dp(16))
    }

    /** Filled (primary) or tonal button. */
    fun button(label: String, filled: Boolean = true, onClick: () -> Unit) = TextView(ctx).apply {
        text = label; textSize = 15f; gravity = Gravity.CENTER
        typeface = Typeface.create("sans-serif-medium", Typeface.NORMAL)
        setTextColor(if (filled) onPrimary else onPrimaryContainer)
        background = ripple(rounded(if (filled) primary else secondaryContainer, 28), 28)
        setPadding(dp(24), dp(14), dp(24), dp(14))
        isClickable = true; isFocusable = true
        setOnClickListener { onClick() }
    }

    fun lp(w: Int = -1, h: Int = -2, top: Int = 0, bottom: Int = 0) =
        LinearLayout.LayoutParams(w, h).apply { topMargin = dp(top); bottomMargin = dp(bottom) }

    fun dot(color: Int, sizeDp: Int = 10) = View(ctx).apply {
        background = GradientDrawable().apply { shape = GradientDrawable.OVAL; setColor(color) }
        layoutParams = LinearLayout.LayoutParams(dp(sizeDp), dp(sizeDp))
    }

    /** Edge-to-edge window with surface-colored system bars; returns insets applier for the root. */
    fun applyWindow(a: Activity, root: View) {
        a.window.statusBarColor = Color.TRANSPARENT
        a.window.navigationBarColor = Color.TRANSPARENT
        a.window.setDecorFitsSystemWindows(false)
        a.window.decorView.setBackgroundColor(surface)
        a.window.insetsController?.setSystemBarsAppearance(
            if (dark) 0 else (android.view.WindowInsetsController.APPEARANCE_LIGHT_STATUS_BARS or
                android.view.WindowInsetsController.APPEARANCE_LIGHT_NAVIGATION_BARS),
            android.view.WindowInsetsController.APPEARANCE_LIGHT_STATUS_BARS or
                android.view.WindowInsetsController.APPEARANCE_LIGHT_NAVIGATION_BARS)
        val l = root.paddingLeft; val t = root.paddingTop; val r = root.paddingRight; val b = root.paddingBottom
        root.setOnApplyWindowInsetsListener { v, ins ->
            val bars = ins.getInsets(WindowInsets.Type.systemBars() or WindowInsets.Type.displayCutout())
            v.setPadding(l + bars.left, t + bars.top, r + bars.right, b + bars.bottom)
            ins
        }
    }
}

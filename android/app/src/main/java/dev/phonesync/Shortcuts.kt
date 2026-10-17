package dev.phonesync

import android.content.Context
import android.content.Intent
import android.content.pm.ShortcutInfo
import android.content.pm.ShortcutManager
import android.graphics.drawable.Icon
import android.util.Log

/**
 * One dynamic, long-lived sharing shortcut per paired PC (category dev.phonesync.SHARE_TARGET, matched by the
 * <share-target> in res/xml/shortcuts.xml), so every PC shows up as a direct-share target in the share sheet.
 */
object Shortcuts {
    private const val TAG = "SymphoneShortcuts"
    const val CATEGORY = "dev.phonesync.SHARE_TARGET"
    private const val PREFIX = "pc_"
    @Volatile private var lastKey = ""

    fun pcIdFrom(shortcutId: String?): String? = shortcutId?.takeIf { it.startsWith(PREFIX) }?.removePrefix(PREFIX)

    fun publish(ctx: Context) {
        val pcs = Pcs.all()
        val key = pcs.joinToString("|") { "${it.id}=${it.name}=${it.master}" }
        if (key == lastKey) return
        try {
            val sm = ctx.getSystemService(ShortcutManager::class.java) ?: return
            val max = sm.maxShortcutCountPerActivity.coerceAtLeast(1)
            val list = pcs.sortedByDescending { it.master }.take(max).mapIndexed { rank, pc ->
                ShortcutInfo.Builder(ctx, PREFIX + pc.id)
                    .setShortLabel(pc.name.take(24))
                    .setLongLabel("Send to ${pc.name}")
                    .setIcon(Icon.createWithResource(ctx, R.drawable.ic_share_pc))
                    .setIntent(Intent(ctx, MainActivity::class.java).setAction(Intent.ACTION_VIEW))
                    .setCategories(setOf(CATEGORY))
                    .setLongLived(true)
                    .setRank(rank)
                    .build()
            }
            val keep = list.map { it.id }.toSet()
            val stale = (sm.dynamicShortcuts + sm.getShortcuts(ShortcutManager.FLAG_MATCH_CACHED))
                .map { it.id }.filter { it.startsWith(PREFIX) && it !in keep }.distinct()
            if (stale.isNotEmpty()) try { sm.removeLongLivedShortcuts(stale) } catch (e: Exception) { Log.w(TAG, "remove: $e") }
            if (sm.setDynamicShortcuts(list)) {
                lastKey = key
                Log.i(TAG, "published ${list.size} share target(s); now ${sm.dynamicShortcuts.size} dynamic")
            } else Log.w(TAG, "rate limited")
        } catch (e: Exception) { Log.w(TAG, "publish: $e") }
    }

    fun reportShared(ctx: Context, pcId: String) {
        try { ctx.getSystemService(ShortcutManager::class.java)?.reportShortcutUsed(PREFIX + pcId) } catch (_: Exception) {}
    }
}

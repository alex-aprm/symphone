package dev.phonesync

import android.app.ActivityOptions
import android.app.Notification
import android.app.PendingIntent
import android.app.RemoteInput
import android.content.Intent
import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.drawable.BitmapDrawable
import android.graphics.drawable.Drawable
import android.os.Build
import android.os.Bundle
import android.service.notification.NotificationListenerService
import android.service.notification.StatusBarNotification
import org.json.JSONArray
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.util.concurrent.ConcurrentHashMap

class NotifListener : NotificationListenerService() {

    override fun onListenerConnected() {
        instance = this
        Hub.startService(this)
    }

    override fun onListenerDisconnected() {
        if (instance === this) instance = null
    }

    override fun onNotificationPosted(sbn: StatusBarNotification) {
        if (!relevant(sbn)) return
        Hub.emit("notif", toJson(sbn))
    }

    override fun onNotificationRemoved(sbn: StatusBarNotification) {
        largeIcons.remove(sbn.key)
        Hub.emit("notif_removed", JSONObject().put("key", sbn.key))
    }

    companion object {
        @Volatile var instance: NotifListener? = null
        private val largeIcons = ConcurrentHashMap<String, ByteArray>()
        private val labels = ConcurrentHashMap<String, String>()

        fun relevant(sbn: StatusBarNotification): Boolean {
            if (sbn.packageName == Hub.ctx.packageName) return false
            val n = sbn.notification
            if (n.flags and Notification.FLAG_GROUP_SUMMARY != 0) return false
            return true
        }

        fun list(): JSONArray {
            val out = JSONArray()
            val l = instance ?: return out
            val active = try { l.activeNotifications } catch (_: Exception) { null } ?: return out
            for (sbn in active.sortedByDescending { it.postTime }) if (relevant(sbn)) out.put(toJson(sbn))
            return out
        }

        private fun label(pkg: String) = labels.getOrPut(pkg) {
            try { Hub.ctx.packageManager.run { getApplicationLabel(getApplicationInfo(pkg, 0)).toString() } } catch (_: Exception) { pkg }
        }

        fun toJson(sbn: StatusBarNotification): JSONObject {
            val n = sbn.notification
            val ex = n.extras
            val title = ex.getCharSequence(Notification.EXTRA_TITLE_BIG) ?: ex.getCharSequence(Notification.EXTRA_TITLE)
            val text = ex.getCharSequence(Notification.EXTRA_BIG_TEXT) ?: ex.getCharSequence(Notification.EXTRA_TEXT)
            val isCall = n.category == Notification.CATEGORY_CALL ||
                ex.getString(Notification.EXTRA_TEMPLATE)?.contains("CallStyle") == true
            val o = JSONObject()
                .put("key", sbn.key)
                .put("pkg", sbn.packageName)
                .put("app", label(sbn.packageName))
                .put("title", title?.toString() ?: "")
                .put("text", text?.toString() ?: "")
                .put("sub", ex.getCharSequence(Notification.EXTRA_SUB_TEXT)?.toString() ?: "")
                .put("when", sbn.postTime)
                .put("ongoing", sbn.isOngoing)
                .put("clearable", sbn.isClearable)
                .put("category", n.category ?: "")
                .put("call", isCall)
                .put("canOpen", n.contentIntent != null)

            // Messaging-style conversation: the last few lines are more useful than the summary text.
            val msgs = ex.getParcelableArray(Notification.EXTRA_MESSAGES, android.os.Parcelable::class.java)
            if (msgs != null && msgs.isNotEmpty()) {
                val arr = JSONArray()
                for (m in msgs.takeLast(6)) {
                    val b = m as? Bundle ?: continue
                    val sender = b.getCharSequence("sender")?.toString()
                        ?: (b.getParcelable("sender_person", android.app.Person::class.java))?.name?.toString() ?: ""
                    arr.put(JSONObject().put("from", sender).put("text", b.getCharSequence("text")?.toString() ?: "").put("time", b.getLong("time")))
                }
                o.put("messages", arr)
            }

            val actions = JSONArray()
            n.actions?.forEachIndexed { i, a ->
                actions.put(JSONObject()
                    .put("i", i)
                    .put("title", a.title?.toString() ?: "")
                    .put("reply", a.remoteInputs?.any { it.allowFreeFormInput } == true))
            }
            o.put("actions", actions)

            n.getLargeIcon()?.let { icon ->
                if (!largeIcons.containsKey(sbn.key)) {
                    try { icon.loadDrawable(Hub.ctx)?.let { largeIcons[sbn.key] = png(it, 128) } } catch (_: Exception) {}
                }
            }
            o.put("hasIcon", largeIcons.containsKey(sbn.key))
            return o
        }

        fun largeIcon(key: String): ByteArray? = largeIcons[key]

        private fun find(key: String): StatusBarNotification? =
            instance?.activeNotifications?.firstOrNull { it.key == key }

        /** PendingIntent.send() reports no error for a bad display, so only target displays that exist. */
        fun displayExists(id: Int): Boolean = try {
            Hub.ctx.getSystemService(android.hardware.display.DisplayManager::class.java)?.getDisplay(id) != null
        } catch (_: Exception) { false }

        /** Background-activity-start options, optionally targeting another display (e.g. a virtual/sidecar one). */
        fun launchOptions(display: Int?): Bundle {
            val o = ActivityOptions.makeBasic()
            if (display != null && displayExists(display)) o.setLaunchDisplayId(display)
            if (Build.VERSION.SDK_INT >= 36) {
                o.setPendingIntentBackgroundActivityStartMode(ActivityOptions.MODE_BACKGROUND_ACTIVITY_START_ALLOW_ALWAYS)
            } else {
                @Suppress("DEPRECATION")
                o.setPendingIntentBackgroundActivityStartMode(ActivityOptions.MODE_BACKGROUND_ACTIVITY_START_ALLOWED)
            }
            return o.toBundle()
        }

        private fun send(pi: PendingIntent, fill: Intent? = null, display: Int? = null) {
            if (display != null && displayExists(display)) {
                try {
                    pi.send(Hub.ctx, 0, fill, null, null, null, launchOptions(display))
                    return
                } catch (e: PendingIntent.CanceledException) {
                    throw e
                } catch (e: Exception) {
                    android.util.Log.w("Symphone", "send on display $display failed, retrying without: $e")
                }
            }
            pi.send(Hub.ctx, 0, fill, null, null, null, launchOptions(null))
        }

        /** op: open | dismiss | action (with index, optional reply text). Returns error string or null. */
        fun act(key: String, op: String, index: Int, text: String?, display: Int? = null): String? {
            val l = instance ?: return "notification access not granted"
            val sbn = find(key) ?: return "notification gone"
            val n = sbn.notification
            when (op) {
                "open" -> {
                    val pi = n.contentIntent ?: return "no content intent"
                    send(pi, null, display)
                    if (n.flags and Notification.FLAG_AUTO_CANCEL != 0) l.cancelNotification(key)
                }
                "dismiss" -> l.cancelNotification(key)
                "action" -> {
                    val a = n.actions?.getOrNull(index) ?: return "no such action"
                    val inputs = a.remoteInputs
                    if (!text.isNullOrEmpty() && inputs != null && inputs.isNotEmpty()) {
                        val fill = Intent()
                        val results = Bundle()
                        for (ri in inputs) results.putCharSequence(ri.resultKey, text)
                        RemoteInput.addResultsToIntent(inputs, fill, results)
                        RemoteInput.setResultsSource(fill, RemoteInput.SOURCE_FREE_FORM_INPUT)
                        send(a.actionIntent, fill, display)
                    } else send(a.actionIntent, null, display)
                }
                else -> return "unknown op"
            }
            return null
        }

        fun png(d: Drawable, max: Int): ByteArray {
            val bmp = if (d is BitmapDrawable && d.bitmap != null) {
                val b = d.bitmap
                if (b.width > max || b.height > max) Bitmap.createScaledBitmap(b, max, max * b.height / maxOf(1, b.width), true) else b
            } else {
                val w = d.intrinsicWidth.takeIf { it > 0 }?.coerceAtMost(max) ?: max
                val h = d.intrinsicHeight.takeIf { it > 0 }?.coerceAtMost(max) ?: max
                Bitmap.createBitmap(w, h, Bitmap.Config.ARGB_8888).also {
                    val c = Canvas(it); d.setBounds(0, 0, w, h); d.draw(c)
                }
            }
            val out = ByteArrayOutputStream()
            bmp.compress(Bitmap.CompressFormat.PNG, 100, out)
            return out.toByteArray()
        }
    }
}

package dev.phonesync

import android.app.Activity
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.provider.OpenableColumns
import android.widget.Toast
import org.json.JSONObject

/**
 * Share-sheet target "Send to PC": files are staged locally and pulled by the PC over the fastest link.
 * Picked through a per-PC direct-share shortcut (EXTRA_SHORTCUT_ID) it goes to that PC, otherwise to the master.
 */
class ShareActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        Hub.startService(this)
        val target: String? = Shortcuts.pcIdFrom(intent.getStringExtra(Intent.EXTRA_SHORTCUT_ID))?.takeIf { Pcs.byId(it) != null }
        val targetPc = Pcs.byId(target) ?: Pcs.master
        val pcLabel = targetPc?.name ?: "PC"
        if (target != null) Shortcuts.reportShared(this, target)
        val uris = ArrayList<Uri>()
        when (intent.action) {
            Intent.ACTION_SEND -> intent.getParcelableExtra(Intent.EXTRA_STREAM, Uri::class.java)?.let { uris += it }
            Intent.ACTION_SEND_MULTIPLE -> intent.getParcelableArrayListExtra(Intent.EXTRA_STREAM, Uri::class.java)?.let { uris += it }
        }
        val text = intent.getCharSequenceExtra(Intent.EXTRA_TEXT)?.toString()
        if (uris.isEmpty() && text != null) {
            Hub.emitTargeted("share_text", JSONObject().put("text", text)
                .put("subject", intent.getStringExtra(Intent.EXTRA_SUBJECT) ?: "")
                .put("pc", targetPc?.id ?: ""), target)
            Toast.makeText(this, "Sent to $pcLabel", Toast.LENGTH_SHORT).show()
            finish(); return
        }
        if (uris.isEmpty()) { finish(); return }
        // Copy while we still hold the URI grant; large files continue in the background.
        val cr = contentResolver
        val items = uris.map { u -> u to displayName(u) }
        Thread {
            var base = System.currentTimeMillis()
            for ((u, name) in items) {
                val id = (base++).toString()
                val f = Shares.fileFor(id, name, target)
                try {
                    cr.openInputStream(u)?.use { inp -> f.outputStream().use { inp.copyTo(it, 1 shl 20) } }
                    Shares.add(Share(id, name, cr.getType(u) ?: Http.mime(name), f, target))
                } catch (_: Exception) { f.delete() }
            }
        }.start()
        val online = targetPc?.let { Hub.hasSubscriber(it.id) } ?: (Hub.subscriberCount > 0)
        val msg = if (online) "Sending ${uris.size} item(s) to $pcLabel" else "Queued ${uris.size} item(s) for $pcLabel"
        Toast.makeText(this, msg, Toast.LENGTH_SHORT).show()
        finish()
    }

    private fun displayName(u: Uri): String {
        try {
            contentResolver.query(u, arrayOf(OpenableColumns.DISPLAY_NAME), null, null, null)?.use { c ->
                if (c.moveToFirst() && !c.isNull(0)) return c.getString(0)
            }
        } catch (_: Exception) {}
        return u.lastPathSegment?.substringAfterLast('/') ?: "shared-${System.currentTimeMillis()}"
    }
}

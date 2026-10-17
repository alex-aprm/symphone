package dev.phonesync

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.BroadcastReceiver
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.net.wifi.WifiManager
import android.os.IBinder
import android.os.PowerManager
import android.util.Log

class SyncService : Service() {
    private var wifiLock: WifiManager.WifiLock? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(NotificationChannel("svc", "Connection", NotificationManager.IMPORTANCE_MIN).apply {
            setShowBadge(false)
        })
        val open = PendingIntent.getActivity(this, 0, Intent(this, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE)
        val n = Notification.Builder(this, "svc")
            .setSmallIcon(R.drawable.ic_stat)
            .setContentTitle("Symphone is ready")
            .setContentText("Reachable from your PC")
            .setContentIntent(open)
            .setOngoing(true)
            // Android only lets apps read the clipboard in the foreground: one tap sends it to the PC
            .addAction(Notification.Action.Builder(null, "Send clipboard to PC",
                PendingIntent.getActivity(this, 2, Intent(this, ClipSendActivity::class.java)
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK), PendingIntent.FLAG_IMMUTABLE)).build())
            .build()
        startForeground(1, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)

        // Keep Wi-Fi in high-performance / low-latency mode while the service runs.
        val wm = applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager
        @Suppress("DEPRECATION")
        wifiLock = wm.createWifiLock(WifiManager.WIFI_MODE_FULL_LOW_LATENCY, "phonesync").apply {
            setReferenceCounted(false); acquire()
        }
        BtLink.start()          // the PC's Bluetooth link ("keep the phone unlocked near this PC")

        try { server.start() } catch (e: Exception) { Log.e("Symphone", "server: $e") }
        try { StreamServer.start() } catch (e: Exception) { Log.e("Symphone", "streams: $e") }
        try { Discovery.start() } catch (e: Exception) { Log.e("Symphone", "discovery: $e") }
        Media.watch()
        Shares.restore()
        try { Torch.init() } catch (e: Exception) { Log.w("Symphone", "torch: $e") }
        try { Arrival.start() } catch (e: Exception) { Log.w("Symphone", "arrival: $e") }
        try { Ble.start() } catch (e: Exception) { Log.w("Symphone", "ble: $e") }
        watchClipboard()
        Pcs.addListener(pcsListener)
        Shortcuts.publish(this)
    }

    private val pcsListener: () -> Unit = {
        MainActivity.refresh()
        Arrival.handler.post { Shortcuts.publish(this) }
    }

    /**
     * Clipboard changes the app itself can see (Android only reports them while we are in the foreground or the
     * default IME; the shell helper ShellClip covers the background case).
     */
    private fun watchClipboard() {
        val cm = getSystemService(ClipboardManager::class.java) ?: return
        cm.addPrimaryClipChangedListener {
            try {
                val clip = cm.primaryClip ?: return@addPrimaryClipChangedListener
                if (clip.description?.label == "PC") return@addPrimaryClipChangedListener // set by a PC: no echo
                val t = clip.takeIf { it.itemCount > 0 }?.getItemAt(0)?.text?.toString()
                Hub.phoneClipChanged(t, "app")
            } catch (e: Exception) { Log.d("Symphone", "clip: $e") }
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int = START_STICKY

    override fun onDestroy() {
        Pcs.removeListener(pcsListener)
        wifiLock?.release()
        super.onDestroy()
    }

    companion object {
        val server = HttpServer(Hub.HTTP_PORT)
    }
}

class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        try { Hub.startService(context) } catch (e: Exception) { Log.w("Symphone", "boot start: $e") }
    }
}

/**
 * Reachable only from adb shell (protected by DUMP): hands the pairing token to a USB-connected PC.
 * `am broadcast -a dev.phonesync.GET_TOKEN -n dev.phonesync/.PairReceiver [--es pc <pc id> --es pcname <name>]`
 * -> result data `token|phoneId|wifiIp`. With `pc` the token belongs to that PC (entry created on first use; the
 * first PC becomes master); without it, to the "legacy" entry (v1 PCs).
 */
class PairReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        try { Hub.startService(context) } catch (_: Exception) {}
        if (intent.action == "dev.phonesync.GET_TOKEN") {
            val pcId = intent.getStringExtra("pc")?.trim().orEmpty()
            val pc = if (pcId.isNotEmpty()) Pcs.getOrCreate(pcId, intent.getStringExtra("pcname")) else Pcs.legacy()
            resultCode = 1
            resultData = "${pc.token}|${Hub.deviceId}|${Arrival.currentIp() ?: ""}"
            MainActivity.refresh()
        }
    }
}

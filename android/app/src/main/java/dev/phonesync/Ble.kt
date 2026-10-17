package dev.phonesync

import android.Manifest
import android.annotation.SuppressLint
import android.bluetooth.BluetoothAdapter
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothGatt
import android.bluetooth.BluetoothGattCharacteristic
import android.bluetooth.BluetoothGattServer
import android.bluetooth.BluetoothGattServerCallback
import android.bluetooth.BluetoothGattService
import android.bluetooth.BluetoothManager
import android.bluetooth.le.AdvertiseCallback
import android.bluetooth.le.AdvertiseData
import android.bluetooth.le.AdvertiseSettings
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.os.Build
import android.os.ParcelUuid
import android.util.Log
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.net.InetAddress
import java.security.MessageDigest
import java.security.SecureRandom
import java.util.UUID
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import javax.crypto.Cipher
import javax.crypto.spec.GCMParameterSpec
import javax.crypto.spec.SecretKeySpec

/**
 * BLE peripheral (protocol v2):
 * - advertises service 5e7c0001-… with 10 bytes of service data: SHA-256(deviceId)[0..6] + Wi-Fi IPv4 (or zeros),
 *   so a PC can spot a known phone nearby and learn its new IP;
 * - GATT server: 0002 READ info JSON, 0003 WRITE pairing request, 0004 READ pairing response (AES-GCM, key SHA-256(secret)).
 * Everything fails soft: no adapter / Bluetooth off / no permission just means "not advertising".
 */
@SuppressLint("MissingPermission")
object Ble {
    private const val TAG = "SymphoneBle"
    val SERVICE: UUID = UUID.fromString("5e7c0001-9b0d-4c55-9e6a-50a0c3e7d6a1")
    val CH_INFO: UUID = UUID.fromString("5e7c0002-9b0d-4c55-9e6a-50a0c3e7d6a1")
    val CH_PAIR_REQ: UUID = UUID.fromString("5e7c0003-9b0d-4c55-9e6a-50a0c3e7d6a1")
    val CH_PAIR_RESP: UUID = UUID.fromString("5e7c0004-9b0d-4c55-9e6a-50a0c3e7d6a1")

    @Volatile var advertising = false
        private set
    @Volatile var status = "off"
        private set

    private var server: BluetoothGattServer? = null
    private var advertisedIp: String? = null
    private var receiverRegistered = false
    private val rnd = SecureRandom()

    /** Prepared (long) writes per device, and the pairing response each device may read. */
    private val pendingWrites = ConcurrentHashMap<String, ByteArrayOutputStream>()
    private val responses = ConcurrentHashMap<String, ByteArray>()

    /** A QR pairing waiting for the PC to come over BLE. */
    class PendingPair(val secret: String, val pcId: String, val pcName: String, val deadline: Long) {
        val done = CountDownLatch(1)
        @Volatile var pc: Pc? = null
    }
    @Volatile private var pending: PendingPair? = null

    private val adapter: BluetoothAdapter?
        get() = try { (Hub.ctx.getSystemService(Context.BLUETOOTH_SERVICE) as? BluetoothManager)?.adapter } catch (_: Exception) { null }

    fun hasPermissions(): Boolean {
        val ctx = Hub.ctx
        return if (Build.VERSION.SDK_INT >= 31) {
            ctx.checkSelfPermission(Manifest.permission.BLUETOOTH_ADVERTISE) == PackageManager.PERMISSION_GRANTED &&
                ctx.checkSelfPermission(Manifest.permission.BLUETOOTH_CONNECT) == PackageManager.PERMISSION_GRANTED
        } else true
    }

    val supported: Boolean
        get() = Hub.ctx.packageManager.hasSystemFeature(PackageManager.FEATURE_BLUETOOTH_LE) && adapter != null

    @Synchronized
    fun start() {
        if (!receiverRegistered) {
            receiverRegistered = true
            try {
                Hub.ctx.registerReceiver(object : BroadcastReceiver() {
                    override fun onReceive(c: Context, i: Intent) {
                        when (i.getIntExtra(BluetoothAdapter.EXTRA_STATE, -1)) {
                            BluetoothAdapter.STATE_ON -> Arrival.handler.post { start() }
                            BluetoothAdapter.STATE_TURNING_OFF, BluetoothAdapter.STATE_OFF -> Arrival.handler.post { stop("bluetooth off") }
                        }
                    }
                }, IntentFilter(BluetoothAdapter.ACTION_STATE_CHANGED))
            } catch (e: Exception) { Log.w(TAG, "receiver: $e") }
        }
        if (server != null) return
        val a = adapter
        when {
            !supported || a == null -> { status = "unsupported"; return }
            !hasPermissions() -> { status = "no permission"; return }
            !a.isEnabled -> { status = "bluetooth off"; return }
        }
        try {
            val bm = Hub.ctx.getSystemService(Context.BLUETOOTH_SERVICE) as BluetoothManager
            val s = bm.openGattServer(Hub.ctx, gattCallback) ?: run { status = "gatt unavailable"; return }
            server = s
            val svc = BluetoothGattService(SERVICE, BluetoothGattService.SERVICE_TYPE_PRIMARY)
            svc.addCharacteristic(BluetoothGattCharacteristic(CH_INFO,
                BluetoothGattCharacteristic.PROPERTY_READ, BluetoothGattCharacteristic.PERMISSION_READ))
            svc.addCharacteristic(BluetoothGattCharacteristic(CH_PAIR_REQ,
                BluetoothGattCharacteristic.PROPERTY_WRITE, BluetoothGattCharacteristic.PERMISSION_WRITE))
            svc.addCharacteristic(BluetoothGattCharacteristic(CH_PAIR_RESP,
                BluetoothGattCharacteristic.PROPERTY_READ, BluetoothGattCharacteristic.PERMISSION_READ))
            s.addService(svc) // advertising starts in onServiceAdded
            status = "starting"
        } catch (e: Exception) {
            Log.w(TAG, "start: $e")
            status = "error: ${e.javaClass.simpleName}"
            stop("error")
        }
    }

    @Synchronized
    fun stop(why: String) {
        try { if (advertising) adapter?.bluetoothLeAdvertiser?.stopAdvertising(advCallback) } catch (_: Exception) {}
        advertising = false
        try { server?.close() } catch (_: Exception) {}
        server = null
        if (status == "advertising" || status == "starting") status = why
        Log.i(TAG, "stopped ($why)")
    }

    /** Our Wi-Fi IP changed (or permissions were granted): restart advertising with fresh service data. */
    fun refresh() {
        Arrival.handler.post {
            if (server == null) { start(); return@post }
            if (Arrival.currentIp() != advertisedIp) startAdvertising()
        }
    }

    private fun serviceData(): ByteArray {
        val h = MessageDigest.getInstance("SHA-256").digest(Hub.deviceId.toByteArray())
        val ip = Arrival.currentIp()
        val ipb = try { ip?.let { InetAddress.getByName(it).address }?.takeIf { it.size == 4 } } catch (_: Exception) { null } ?: ByteArray(4)
        return h.copyOf(6) + ipb
    }

    @Synchronized
    private fun startAdvertising() {
        val adv = adapter?.bluetoothLeAdvertiser ?: run { status = "no advertiser"; return }
        try {
            if (advertising) adv.stopAdvertising(advCallback)
            advertising = false
            val settings = AdvertiseSettings.Builder()
                .setAdvertiseMode(AdvertiseSettings.ADVERTISE_MODE_LOW_POWER)
                .setTxPowerLevel(AdvertiseSettings.ADVERTISE_TX_POWER_MEDIUM)
                .setConnectable(true)
                .setTimeout(0)
                .build()
            // 3 (flags) + 2 + 16 (UUID) + 10 (data) = 31 bytes: exactly one legacy advertisement.
            val data = AdvertiseData.Builder()
                .setIncludeDeviceName(false).setIncludeTxPowerLevel(false)
                .addServiceData(ParcelUuid(SERVICE), serviceData())
                .build()
            val scan = AdvertiseData.Builder()
                .setIncludeDeviceName(false).setIncludeTxPowerLevel(false)
                .addServiceUuid(ParcelUuid(SERVICE))
                .build()
            advertisedIp = Arrival.currentIp()
            adv.startAdvertising(settings, data, scan, advCallback)
        } catch (e: Exception) {
            Log.w(TAG, "advertise: $e")
            status = "error: ${e.javaClass.simpleName}"
        }
    }

    private val advCallback = object : AdvertiseCallback() {
        override fun onStartSuccess(settingsInEffect: AdvertiseSettings) {
            advertising = true; status = "advertising"
            Log.i(TAG, "advertising (ip=$advertisedIp)")
            MainActivity.refresh()
        }
        override fun onStartFailure(errorCode: Int) {
            advertising = false; status = "advertise failed ($errorCode)"
            Log.w(TAG, "advertise failed $errorCode")
            MainActivity.refresh()
        }
    }

    // ---------------------------------------------------------------- pairing
    /** Waits (blocking) for a PC to complete QR pairing over BLE. Returns the paired PC or null on timeout. */
    fun awaitPairing(secret: String, pcId: String, pcName: String, timeoutMs: Long): Pc? {
        val p = PendingPair(secret, pcId, pcName, System.currentTimeMillis() + timeoutMs)
        pending = p
        start()
        try {
            p.done.await(timeoutMs, TimeUnit.MILLISECONDS)
        } finally {
            if (pending === p) pending = null
        }
        return p.pc
    }

    fun cancelPairing() { pending?.done?.countDown(); pending = null }

    private fun key(secret: String) = SecretKeySpec(MessageDigest.getInstance("SHA-256").digest(secret.toByteArray()), "AES")

    private fun seal(secret: String, plain: ByteArray): ByteArray {
        val nonce = ByteArray(12).also { rnd.nextBytes(it) }
        val c = Cipher.getInstance("AES/GCM/NoPadding")
        c.init(Cipher.ENCRYPT_MODE, key(secret), GCMParameterSpec(128, nonce))
        return nonce + c.doFinal(plain)
    }

    private fun open(secret: String, data: ByteArray): ByteArray {
        require(data.size > 12 + 16) { "short" }
        val c = Cipher.getInstance("AES/GCM/NoPadding")
        c.init(Cipher.DECRYPT_MODE, key(secret), GCMParameterSpec(128, data, 0, 12))
        return c.doFinal(data, 12, data.size - 12)
    }

    /** Handles a complete 0003 write. Returns true if accepted. */
    private fun onPairRequest(dev: BluetoothDevice, value: ByteArray): Boolean {
        val p = pending ?: run { Log.i(TAG, "pair write without pending pairing"); return false }
        if (System.currentTimeMillis() > p.deadline) return false
        val req = try { JSONObject(String(open(p.secret, value))) } catch (e: Exception) {
            Log.w(TAG, "pair decrypt failed: $e"); return false
        }
        val pcId = req.optString("pc")
        if (p.pcId.isNotEmpty() && pcId.isNotEmpty() && pcId != p.pcId) { Log.w(TAG, "pair: pc mismatch"); return false }
        val pc = Pcs.getOrCreate(pcId.ifEmpty { p.pcId }, req.optString("n").ifBlank { p.pcName })
        val ip = Arrival.currentIp() ?: ""
        val resp = JSONObject().put("id", Hub.deviceId).put("name", Hub.deviceName).put("model", Build.MODEL)
            .put("token", pc.token).put("ip", ip).put("port", Hub.HTTP_PORT)
        responses[dev.address] = seal(p.secret, resp.toString().toByteArray())
        p.pc = pc
        p.done.countDown() // the response stays readable (per device) after the pairing wait ends
        Log.i(TAG, "paired ${pc.name} over BLE")
        return true
    }

    private fun infoJson(): ByteArray = JSONObject().put("id", Hub.deviceId).put("name", Hub.deviceName)
        .put("ip", Arrival.currentIp() ?: "").put("port", Hub.HTTP_PORT).toString().toByteArray()

    private val gattCallback = object : BluetoothGattServerCallback() {
        override fun onServiceAdded(status: Int, service: BluetoothGattService) {
            if (status == BluetoothGatt.GATT_SUCCESS) Arrival.handler.post { startAdvertising() }
            else Log.w(TAG, "service add failed $status")
        }

        override fun onConnectionStateChange(device: BluetoothDevice, status: Int, newState: Int) {
            if (newState == android.bluetooth.BluetoothProfile.STATE_DISCONNECTED) {
                pendingWrites.remove(device.address)
            }
        }

        override fun onCharacteristicReadRequest(device: BluetoothDevice, requestId: Int, offset: Int, ch: BluetoothGattCharacteristic) {
            val s = server ?: return
            val value: ByteArray? = when (ch.uuid) {
                CH_INFO -> infoJson()
                CH_PAIR_RESP -> responses[device.address] ?: ByteArray(0) // empty until the pairing write succeeded
                else -> null
            }
            if (value == null) { s.sendResponse(device, requestId, BluetoothGatt.GATT_READ_NOT_PERMITTED, offset, null); return }
            if (offset > value.size) { s.sendResponse(device, requestId, BluetoothGatt.GATT_INVALID_OFFSET, offset, null); return }
            s.sendResponse(device, requestId, BluetoothGatt.GATT_SUCCESS, offset, value.copyOfRange(offset, value.size))
        }

        override fun onCharacteristicWriteRequest(device: BluetoothDevice, requestId: Int, ch: BluetoothGattCharacteristic,
                                                  preparedWrite: Boolean, responseNeeded: Boolean, offset: Int, value: ByteArray?) {
            val s = server ?: return
            if (ch.uuid != CH_PAIR_REQ) {
                if (responseNeeded) s.sendResponse(device, requestId, BluetoothGatt.GATT_WRITE_NOT_PERMITTED, offset, null)
                return
            }
            val v = value ?: ByteArray(0)
            if (preparedWrite) {
                val buf = pendingWrites.getOrPut(device.address) { ByteArrayOutputStream() }
                if (offset != buf.size()) {
                    if (responseNeeded) s.sendResponse(device, requestId, BluetoothGatt.GATT_INVALID_OFFSET, offset, null)
                    return
                }
                buf.write(v)
                if (responseNeeded) s.sendResponse(device, requestId, BluetoothGatt.GATT_SUCCESS, offset, v)
                return
            }
            val ok = onPairRequest(device, v)
            if (responseNeeded) s.sendResponse(device, requestId, if (ok) BluetoothGatt.GATT_SUCCESS else BluetoothGatt.GATT_FAILURE, offset, null)
        }

        override fun onExecuteWrite(device: BluetoothDevice, requestId: Int, execute: Boolean) {
            val s = server ?: return
            val buf = pendingWrites.remove(device.address)
            if (!execute || buf == null) { s.sendResponse(device, requestId, BluetoothGatt.GATT_SUCCESS, 0, null); return }
            val ok = onPairRequest(device, buf.toByteArray())
            s.sendResponse(device, requestId, if (ok) BluetoothGatt.GATT_SUCCESS else BluetoothGatt.GATT_FAILURE, 0, null)
        }
    }
}

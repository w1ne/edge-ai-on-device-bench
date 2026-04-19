package dev.robot.companion

import android.Manifest
import android.annotation.SuppressLint
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.bluetooth.BluetoothAdapter
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothGatt
import android.bluetooth.BluetoothGattCallback
import android.bluetooth.BluetoothGattCharacteristic
import android.bluetooth.BluetoothGattDescriptor
import android.bluetooth.BluetoothManager
import android.bluetooth.BluetoothProfile
import android.bluetooth.le.BluetoothLeScanner
import android.bluetooth.le.ScanCallback
import android.bluetooth.le.ScanFilter
import android.bluetooth.le.ScanResult
import android.bluetooth.le.ScanSettings
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.ParcelUuid
import android.util.Log
import androidx.core.app.NotificationCompat
import androidx.core.content.ContextCompat
import java.util.UUID
import java.util.concurrent.ConcurrentLinkedQueue
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger

/**
 * Foreground service: BLE central to Nordic UART Service + local TCP server at 127.0.0.1:5557.
 * Newline-delimited JSON lines bridge both directions.
 */
class BleRobotService : Service() {

    companion object {
        private const val TAG = "BleRobotSvc"

        const val ACTION_CMD_START = "dev.robot.companion.CMD_START"
        const val ACTION_CMD_DISCONNECT = "dev.robot.companion.CMD_DISCONNECT"

        const val ACTION_STATUS = "dev.robot.companion.STATUS"
        const val ACTION_BLE_MESSAGE = "dev.robot.companion.BLE_MSG"
        const val EXTRA_BLE_STATUS = "ble"
        const val EXTRA_TCP_STATUS = "tcp"
        const val EXTRA_LINE = "line"

        private const val CHANNEL_ID = "ble_robot_fg"
        private const val NOTIF_ID = 1
        private const val TCP_PORT = 5557

        private const val TARGET_NAME = "PhoneWalker-BLE"
        private val NUS_SERVICE = UUID.fromString("6E400001-B5A3-F393-E0A9-E50E24DCCA9E")
        private val NUS_RX = UUID.fromString("6E400002-B5A3-F393-E0A9-E50E24DCCA9E")
        private val NUS_TX = UUID.fromString("6E400003-B5A3-F393-E0A9-E50E24DCCA9E")
        private val CCCD = UUID.fromString("00002902-0000-1000-8000-00805F9B34FB")

        private const val MTU_REQUEST = 247
        private const val QUEUE_WAIT_MS = 2000L
    }

    private val main = Handler(Looper.getMainLooper())

    private var gatt: BluetoothGatt? = null
    private var rxChar: BluetoothGattCharacteristic? = null
    private var scanner: BluetoothLeScanner? = null
    private var scanning = false

    private var bleState = "idle"
    private var reconnectAttempt = 0

    private val tcpClients = AtomicInteger(0)
    private var tcpServer: LocalTcpServer? = null

    /** Outbound queue for TCP -> BLE while BLE is not yet connected. */
    private data class PendingWrite(val line: ByteArray, val addedAt: Long)

    private val pending = ConcurrentLinkedQueue<PendingWrite>()
    private val bleBusy = AtomicBoolean(false)

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        createNotificationChannel()
        startForegroundWithNotification()
        startTcp()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        Log.i(TAG, "onStartCommand action=${intent?.action}")
        when (intent?.action) {
            ACTION_CMD_DISCONNECT -> {
                disconnectGatt()
                stopScan()
                setBleState("disconnected")
                updateNotification()
            }
            else -> {
                if (gatt == null && !scanning) startScan()
            }
        }
        return START_STICKY
    }

    override fun onDestroy() {
        super.onDestroy()
        stopScan()
        disconnectGatt()
        tcpServer?.stop()
        tcpServer = null
    }

    // -------- notification / status --------

    private fun createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val ch = NotificationChannel(
                CHANNEL_ID,
                "Robot BLE bridge",
                NotificationManager.IMPORTANCE_LOW
            )
            getSystemService(NotificationManager::class.java).createNotificationChannel(ch)
        }
    }

    private fun buildNotification(): Notification {
        val pi = PendingIntent.getActivity(
            this, 0,
            Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )
        val text = "BLE: $bleState, TCP: ${tcpClients.get()} clients"
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle("Robot companion")
            .setContentText(text)
            .setSmallIcon(android.R.drawable.stat_sys_data_bluetooth)
            .setContentIntent(pi)
            .setOngoing(true)
            .build()
    }

    private fun startForegroundWithNotification() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startForeground(
                NOTIF_ID,
                buildNotification(),
                android.content.pm.ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE
            )
        } else {
            startForeground(NOTIF_ID, buildNotification())
        }
    }

    private fun updateNotification() {
        getSystemService(NotificationManager::class.java).notify(NOTIF_ID, buildNotification())
        broadcastStatus()
    }

    private fun broadcastStatus() {
        sendBroadcast(
            Intent(ACTION_STATUS).setPackage(packageName)
                .putExtra(EXTRA_BLE_STATUS, bleState)
                .putExtra(EXTRA_TCP_STATUS, "${tcpClients.get()} clients")
        )
    }

    private fun setBleState(s: String) {
        bleState = s
        updateNotification()
    }

    // -------- TCP server --------

    private fun startTcp() {
        tcpServer = LocalTcpServer(
            port = TCP_PORT,
            onClientCountChanged = { n ->
                tcpClients.set(n)
                main.post { updateNotification() }
            },
            onLineFromClient = { line ->
                handleLineFromTcp(line)
            }
        ).also { it.start() }
    }

    private fun broadcastBleLine(line: String) {
        tcpServer?.broadcastLine(line)
        sendBroadcast(
            Intent(ACTION_BLE_MESSAGE).setPackage(packageName)
                .putExtra(EXTRA_LINE, line)
        )
    }

    // -------- TCP -> BLE --------

    private fun handleLineFromTcp(line: String) {
        if (line.isBlank()) return
        val bytes = (line + "\n").toByteArray(Charsets.UTF_8)

        val g = gatt
        val rx = rxChar
        if (g != null && rx != null) {
            enqueueAndWrite(bytes)
        } else {
            // Queue for up to QUEUE_WAIT_MS, else return error.
            pending.add(PendingWrite(bytes, System.nanoTime() / 1_000_000))
            main.postDelayed({ flushOrErrorPending() }, QUEUE_WAIT_MS + 50)
        }
    }

    private fun flushOrErrorPending() {
        val now = System.nanoTime() / 1_000_000
        val g = gatt
        val rx = rxChar
        val iter = pending.iterator()
        while (iter.hasNext()) {
            val p = iter.next()
            if (g != null && rx != null) {
                iter.remove()
                writeToBleChunked(p.line)
            } else if (now - p.addedAt >= QUEUE_WAIT_MS) {
                iter.remove()
                tcpServer?.broadcastLine("""{"t":"err","msg":"ble_not_connected"}""")
            }
        }
    }

    private fun enqueueAndWrite(bytes: ByteArray) {
        pending.add(PendingWrite(bytes, System.nanoTime() / 1_000_000))
        tryFlushPendingToBle()
    }

    @SuppressLint("MissingPermission")
    private fun tryFlushPendingToBle() {
        if (gatt == null || rxChar == null) return
        if (!bleBusy.compareAndSet(false, true)) return
        val p = pending.poll()
        if (p == null) {
            bleBusy.set(false)
            return
        }
        if (!writeToBleChunked(p.line)) {
            bleBusy.set(false)
        }
    }

    @SuppressLint("MissingPermission")
    private fun writeToBleChunked(bytes: ByteArray): Boolean {
        val g = gatt ?: return false
        val rx = rxChar ?: return false
        if (!hasConnectPerm()) return false
        // MTU negotiated to 247; ATT payload is MTU-3 = 244. Split if larger.
        val max = (negotiatedMtu - 3).coerceAtLeast(20)
        var offset = 0
        while (offset < bytes.size) {
            val end = (offset + max).coerceAtMost(bytes.size)
            val chunk = bytes.copyOfRange(offset, end)
            val ok = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                g.writeCharacteristic(
                    rx, chunk,
                    BluetoothGattCharacteristic.WRITE_TYPE_NO_RESPONSE
                ) == BluetoothGatt.GATT_SUCCESS
            } else {
                @Suppress("DEPRECATION")
                rx.writeType = BluetoothGattCharacteristic.WRITE_TYPE_NO_RESPONSE
                @Suppress("DEPRECATION")
                rx.value = chunk
                @Suppress("DEPRECATION")
                g.writeCharacteristic(rx)
            }
            if (!ok) {
                Log.w(TAG, "writeCharacteristic failed at offset=$offset")
                bleBusy.set(false)
                return false
            }
            offset = end
        }
        // Write-without-response: callback not guaranteed before next write.
        bleBusy.set(false)
        // Kick off another if queue still has items.
        main.post { tryFlushPendingToBle() }
        return true
    }

    // -------- BLE scan + GATT --------

    private var negotiatedMtu = 23

    private fun hasConnectPerm(): Boolean {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.S) return true
        return ContextCompat.checkSelfPermission(this, Manifest.permission.BLUETOOTH_CONNECT) ==
                PackageManager.PERMISSION_GRANTED
    }

    private fun hasScanPerm(): Boolean {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.S) {
            return ContextCompat.checkSelfPermission(
                this, Manifest.permission.ACCESS_FINE_LOCATION
            ) == PackageManager.PERMISSION_GRANTED
        }
        return ContextCompat.checkSelfPermission(this, Manifest.permission.BLUETOOTH_SCAN) ==
                PackageManager.PERMISSION_GRANTED
    }

    @SuppressLint("MissingPermission")
    private fun startScan() {
        if (!hasScanPerm()) {
            Log.w(TAG, "startScan: BLUETOOTH_SCAN not granted")
            setBleState("no_permission")
            return
        }
        val mgr = getSystemService(BluetoothManager::class.java)
        val adapter: BluetoothAdapter? = mgr?.adapter
        if (adapter == null || !adapter.isEnabled) {
            Log.w(TAG, "startScan: bluetooth adapter off; will retry")
            setBleState("bt_off")
            main.postDelayed({
                if (gatt == null && !scanning) startScan()
            }, 3000)
            return
        }
        val s = adapter.bluetoothLeScanner
        if (s == null) {
            Log.w(TAG, "startScan: no LE scanner")
            setBleState("no_scanner")
            return
        }
        scanner = s
        val filters = listOf(
            ScanFilter.Builder()
                .setServiceUuid(ParcelUuid(NUS_SERVICE))
                .build()
        )
        val settings = ScanSettings.Builder()
            .setScanMode(ScanSettings.SCAN_MODE_LOW_LATENCY)
            .build()
        try {
            s.startScan(filters, settings, scanCallback)
            scanning = true
            setBleState("scanning")
            Log.i(TAG, "scan started for NUS")
        } catch (e: Throwable) {
            Log.e(TAG, "startScan failed", e)
            setBleState("scan_err")
        }
    }

    @SuppressLint("MissingPermission")
    private fun stopScan() {
        if (!scanning) return
        try { scanner?.stopScan(scanCallback) } catch (_: Throwable) {}
        scanning = false
    }

    private val scanCallback = object : ScanCallback() {
        override fun onScanResult(callbackType: Int, result: ScanResult) {
            val dev = result.device ?: return
            val name = try {
                if (hasConnectPerm()) dev.name else null
            } catch (_: SecurityException) { null }
            // Prefer the target name but accept any device advertising the NUS service.
            if (name == null || name == TARGET_NAME) {
                Log.i(TAG, "scan hit: name=$name addr=${dev.address}")
                stopScan()
                connectGatt(dev)
            }
        }

        override fun onScanFailed(errorCode: Int) {
            Log.w(TAG, "scan failed code=$errorCode")
            setBleState("scan_failed")
        }
    }

    @SuppressLint("MissingPermission")
    private fun connectGatt(dev: BluetoothDevice) {
        if (!hasConnectPerm()) { setBleState("no_permission"); return }
        setBleState("connecting")
        gatt = dev.connectGatt(this, false, gattCallback, BluetoothDevice.TRANSPORT_LE)
    }

    @SuppressLint("MissingPermission")
    private fun disconnectGatt() {
        try {
            if (hasConnectPerm()) gatt?.disconnect()
            gatt?.close()
        } catch (_: Throwable) {}
        gatt = null
        rxChar = null
        negotiatedMtu = 23
    }

    private val gattCallback = object : BluetoothGattCallback() {
        @SuppressLint("MissingPermission")
        override fun onConnectionStateChange(g: BluetoothGatt, status: Int, newState: Int) {
            if (newState == BluetoothProfile.STATE_CONNECTED) {
                reconnectAttempt = 0
                setBleState("connected_discovering")
                if (hasConnectPerm()) g.discoverServices()
            } else if (newState == BluetoothProfile.STATE_DISCONNECTED) {
                setBleState("disconnected")
                disconnectGatt()
                scheduleReconnect()
            }
        }

        @SuppressLint("MissingPermission")
        override fun onServicesDiscovered(g: BluetoothGatt, status: Int) {
            if (status != BluetoothGatt.GATT_SUCCESS) {
                setBleState("svc_err")
                return
            }
            val svc = g.getService(NUS_SERVICE)
            if (svc == null) {
                setBleState("no_nus")
                return
            }
            val rx = svc.getCharacteristic(NUS_RX)
            val tx = svc.getCharacteristic(NUS_TX)
            if (rx == null || tx == null) {
                setBleState("no_nus_chars")
                return
            }
            rxChar = rx
            if (hasConnectPerm()) {
                g.requestMtu(MTU_REQUEST)
                g.setCharacteristicNotification(tx, true)
                val desc = tx.getDescriptor(CCCD)
                if (desc != null) {
                    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                        g.writeDescriptor(desc, BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE)
                    } else {
                        @Suppress("DEPRECATION")
                        desc.value = BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE
                        @Suppress("DEPRECATION")
                        g.writeDescriptor(desc)
                    }
                }
            }
            setBleState("connected")
            // Flush any pending writes now that BLE is up.
            main.post { tryFlushPendingToBle() }
        }

        override fun onMtuChanged(g: BluetoothGatt, mtu: Int, status: Int) {
            if (status == BluetoothGatt.GATT_SUCCESS) {
                negotiatedMtu = mtu
                Log.i(TAG, "MTU -> $mtu")
            } else {
                Log.w(TAG, "MTU request failed status=$status; staying at 23")
            }
        }

        // Android 13+ delivers bytes via the new overload.
        override fun onCharacteristicChanged(
            g: BluetoothGatt,
            characteristic: BluetoothGattCharacteristic,
            value: ByteArray
        ) {
            if (characteristic.uuid == NUS_TX) handleIncoming(value)
        }

        // Pre-13 path.
        @Suppress("DEPRECATION", "OVERRIDE_DEPRECATION")
        override fun onCharacteristicChanged(
            g: BluetoothGatt,
            characteristic: BluetoothGattCharacteristic
        ) {
            if (characteristic.uuid == NUS_TX) {
                val v = characteristic.value ?: return
                handleIncoming(v)
            }
        }
    }

    /** Buffer BLE TX bytes and emit newline-delimited lines. */
    private val rxBuffer = StringBuilder()

    private fun handleIncoming(bytes: ByteArray) {
        val chunk = String(bytes, Charsets.UTF_8)
        rxBuffer.append(chunk)
        while (true) {
            val nl = rxBuffer.indexOf('\n')
            if (nl < 0) break
            val line = rxBuffer.substring(0, nl).trimEnd('\r')
            rxBuffer.delete(0, nl + 1)
            if (line.isNotBlank()) broadcastBleLine(line)
        }
        // Guard against unbounded growth if peer never sends a newline.
        if (rxBuffer.length > 8192) {
            val line = rxBuffer.toString()
            rxBuffer.setLength(0)
            broadcastBleLine(line)
        }
    }

    private fun scheduleReconnect() {
        val delayMs = (1_000L shl reconnectAttempt.coerceAtMost(5)).coerceAtMost(30_000L)
        reconnectAttempt++
        Log.i(TAG, "reconnect in ${delayMs}ms (attempt=$reconnectAttempt)")
        main.postDelayed({
            if (gatt == null && !scanning) startScan()
        }, delayMs)
    }
}

package dev.robot.companion

import android.Manifest
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import dev.robot.companion.databinding.ActivityMainBinding

class MainActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMainBinding

    private val recentBleLines = ArrayDeque<String>(8)

    private val receiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context?, intent: Intent?) {
            when (intent?.action) {
                BleRobotService.ACTION_STATUS -> {
                    val ble = intent.getStringExtra(BleRobotService.EXTRA_BLE_STATUS) ?: "?"
                    val tcp = intent.getStringExtra(BleRobotService.EXTRA_TCP_STATUS) ?: "?"
                    binding.bleStatus.text = "BLE: $ble"
                    binding.tcpStatus.text = "TCP: $tcp"
                }
                BleRobotService.ACTION_BLE_MESSAGE -> {
                    val line = intent.getStringExtra(BleRobotService.EXTRA_LINE) ?: return
                    if (recentBleLines.size >= 5) recentBleLines.removeFirst()
                    recentBleLines.addLast(line)
                    binding.bleLog.text = recentBleLines.joinToString("\n")
                }
            }
        }
    }

    private val permsLauncher = registerForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { grants ->
        val allGood = grants.values.all { it }
        if (!allGood) {
            Toast.makeText(
                this,
                "BLE permissions needed. Scanning may fail.",
                Toast.LENGTH_LONG
            ).show()
        }
        startBleService()
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)

        binding.btnScan.setOnClickListener {
            requestPermsAndStart()
        }
        binding.btnDisconnect.setOnClickListener {
            startService(
                Intent(this, BleRobotService::class.java)
                    .setAction(BleRobotService.ACTION_CMD_DISCONNECT)
            )
        }

        requestPermsAndStart()
    }

    override fun onStart() {
        super.onStart()
        val f = IntentFilter().apply {
            addAction(BleRobotService.ACTION_STATUS)
            addAction(BleRobotService.ACTION_BLE_MESSAGE)
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            registerReceiver(receiver, f, Context.RECEIVER_NOT_EXPORTED)
        } else {
            @Suppress("UnspecifiedRegisterReceiverFlag")
            registerReceiver(receiver, f)
        }
    }

    override fun onStop() {
        super.onStop()
        try { unregisterReceiver(receiver) } catch (_: Throwable) {}
    }

    private fun requestPermsAndStart() {
        val needed = mutableListOf<String>()
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            if (ContextCompat.checkSelfPermission(this, Manifest.permission.BLUETOOTH_SCAN)
                != PackageManager.PERMISSION_GRANTED
            ) needed += Manifest.permission.BLUETOOTH_SCAN
            if (ContextCompat.checkSelfPermission(this, Manifest.permission.BLUETOOTH_CONNECT)
                != PackageManager.PERMISSION_GRANTED
            ) needed += Manifest.permission.BLUETOOTH_CONNECT
        } else {
            // API < 31 also wants FINE_LOCATION for BLE scan
            if (ContextCompat.checkSelfPermission(this, Manifest.permission.ACCESS_FINE_LOCATION)
                != PackageManager.PERMISSION_GRANTED
            ) needed += Manifest.permission.ACCESS_FINE_LOCATION
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            if (ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS)
                != PackageManager.PERMISSION_GRANTED
            ) needed += Manifest.permission.POST_NOTIFICATIONS
        }

        if (needed.isNotEmpty()) {
            permsLauncher.launch(needed.toTypedArray())
        } else {
            startBleService()
        }
    }

    private fun startBleService() {
        val i = Intent(this, BleRobotService::class.java)
            .setAction(BleRobotService.ACTION_CMD_START)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            startForegroundService(i)
        } else {
            startService(i)
        }
    }
}

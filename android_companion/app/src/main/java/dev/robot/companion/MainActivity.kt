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
import androidx.lifecycle.lifecycleScope
import dev.robot.companion.databinding.ActivityMainBinding
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONObject

class MainActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMainBinding
    private lateinit var orch: Orchestrator
    private var voice: VoiceListener? = null
    private var voiceActive = false
    private var walkActive = false

    private val receiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context?, intent: Intent?) {
            when (intent?.action) {
                BleRobotService.ACTION_STATUS -> {
                    val ble = intent.getStringExtra(BleRobotService.EXTRA_BLE_STATUS) ?: "?"
                    RobotState.update { it.copy(bleStatus = ble) }
                }
                BleRobotService.ACTION_BLE_MESSAGE -> {
                    val line = intent.getStringExtra(BleRobotService.EXTRA_LINE) ?: return
                    RobotState.appendLog("[ble] $line")
                    // Parse battery voltage if present.
                    try {
                        val j = JSONObject(line)
                        val v = j.optDouble("batt_v", -1.0)
                        if (v > 0) RobotState.update { it.copy(battV = v.toFloat()) }
                        // Convert structured vision/event payloads into goalkeeper events.
                        val t = j.optString("t", "")
                        if (t == "event" || j.has("class") || j.has("seen")) {
                            orch.pushVisionEvent(j)
                        }
                    } catch (_: Throwable) {}
                }
            }
        }
    }

    private val permsLauncher = registerForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { grants ->
        val allGood = grants.values.all { it }
        if (!allGood) {
            Toast.makeText(this,
                "Some permissions denied. Features may degrade.",
                Toast.LENGTH_LONG).show()
        }
        startBleService()
        setupVision()
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)

        orch = Orchestrator.getOrInit(applicationContext)

        binding.btnScan.setOnClickListener { requestPermsAndStart() }
        binding.btnDisconnect.setOnClickListener {
            startService(Intent(this, BleRobotService::class.java)
                .setAction(BleRobotService.ACTION_CMD_DISCONNECT))
        }
        binding.btnMic.setOnClickListener { toggleVoice() }
        binding.btnLookFor.setOnClickListener { fireLookFor() }
        binding.btnCancel.setOnClickListener {
            orch.cancelGoal()
            RobotState.appendLog("[ui] goal cancel requested")
        }
        binding.btnSaveSettings.setOnClickListener { saveSettings() }

        // Remote-control buttons (direct-drive, bypass voice/planner).
        binding.btnPoseNeutral.setOnClickListener    { firePose("neutral") }
        binding.btnPoseLeanLeft.setOnClickListener   { firePose("lean_left") }
        binding.btnPoseLeanRight.setOnClickListener  { firePose("lean_right") }
        binding.btnPoseBow.setOnClickListener        { firePose("bow_front") }
        binding.btnWalk.setOnClickListener           { toggleWalk() }
        binding.btnJump.setOnClickListener           { fireJump() }
        binding.btnEmergencyStop.setOnClickListener  { emergencyStop() }

        // Populate settings from prefs.
        val cfg = orch.config
        binding.editApiKey.setText(cfg.apiKey)
        binding.editPlannerModel.setText(cfg.plannerModel)
        binding.editWakeWord.setText(cfg.wakeWord)
        binding.cbWakeRequired.isChecked = cfg.wakeRequired
        binding.cbTts.isChecked = cfg.ttsEnabled

        // Observe RobotState for UI refresh.
        lifecycleScope.launch {
            RobotState.state.collect { snap ->
                binding.bleStatus.text = "BLE: ${snap.bleStatus}" +
                    (if (snap.bleMac.isNotEmpty()) "  ${snap.bleMac}" else "")
                binding.battStatus.text = if (snap.battV > 0) "Batt: ${"%.2f".format(snap.battV)} V"
                    else "Batt: --"
                binding.goalStatus.text = "Goal: ${if (snap.goal.isEmpty()) "—" else snap.goal} " +
                    "[${snap.goalState}]"
                binding.saySnapshot.text = if (snap.lastSay.isNotEmpty())
                    "say: \"${snap.lastSay}\"" else ""
            }
        }
        lifecycleScope.launch {
            RobotState.log.collect { lines ->
                binding.bleLog.text = lines.takeLast(20).joinToString("\n")
            }
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

    override fun onDestroy() {
        super.onDestroy()
        voice?.stop()
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
            if (ContextCompat.checkSelfPermission(this, Manifest.permission.ACCESS_FINE_LOCATION)
                != PackageManager.PERMISSION_GRANTED
            ) needed += Manifest.permission.ACCESS_FINE_LOCATION
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            if (ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS)
                != PackageManager.PERMISSION_GRANTED
            ) needed += Manifest.permission.POST_NOTIFICATIONS
        }
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO)
            != PackageManager.PERMISSION_GRANTED
        ) needed += Manifest.permission.RECORD_AUDIO
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA)
            != PackageManager.PERMISSION_GRANTED
        ) needed += Manifest.permission.CAMERA

        if (needed.isNotEmpty()) {
            permsLauncher.launch(needed.toTypedArray())
        } else {
            startBleService()
            setupVision()
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

    private fun setupVision() {
        val cam = ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA) ==
            PackageManager.PERMISSION_GRANTED
        if (cam) {
            orch.attachLifecycle(this, this)
        }
    }

    private fun toggleVoice() {
        val mic = ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO) ==
            PackageManager.PERMISSION_GRANTED
        if (!mic) {
            Toast.makeText(this, "Mic permission missing", Toast.LENGTH_SHORT).show()
            return
        }
        if (voiceActive) {
            voice?.stop()
            voiceActive = false
            binding.btnMic.text = "Mic: start"
            return
        }
        val v = VoiceListener(this, orch.config, onUtterance = { utt ->
            RobotState.appendLog("[ui] submitting goal: \"$utt\"")
            orch.submitGoal(utt)
        })
        v.start()
        voice = v
        voiceActive = true
        binding.btnMic.text = "Mic: stop"
    }

    private fun fireLookFor() {
        val v = orch.vision
        if (v == null) {
            Toast.makeText(this, "Camera not ready", Toast.LENGTH_SHORT).show()
            return
        }
        val phrases = listOf("a person", "a laptop", "a red mug")
        RobotState.appendLog("[ui] look_for $phrases")
        Thread {
            try {
                val r = v.query(phrases)
                RobotState.appendLog("[ui] vision -> $r")
            } catch (e: Throwable) {
                RobotState.appendLog("[ui] vision err: ${e.message}")
            }
        }.start()
    }

    // ---- Remote control helpers ---------------------------------------

    private fun sendWireAsync(cmd: JSONObject, label: String) {
        RobotState.appendLog("[remote] $label")
        lifecycleScope.launch {
            try {
                val ack = withContext(Dispatchers.IO) { orch.wire.send(cmd) }
                RobotState.appendLog("[remote]   ack=$ack")
            } catch (e: Throwable) {
                RobotState.appendLog("[remote]   err: ${e.message}")
            }
        }
    }

    private fun firePose(name: String) {
        val cmd = JSONObject()
            .put("c", "pose")
            .put("n", name)
            .put("d", 800)
        sendWireAsync(cmd, "pose($name, d=800)")
    }

    private fun toggleWalk() {
        if (!walkActive) {
            val cmd = JSONObject()
                .put("c", "walk")
                .put("stride", 150)
                .put("step", 400)
            sendWireAsync(cmd, "walk start")
            walkActive = true
            binding.btnWalk.text = "WALKING…"
            binding.btnWalk.setTextColor(0xFFD32F2F.toInt())
        } else {
            sendWireAsync(JSONObject().put("c", "stop"), "walk stop")
            walkActive = false
            binding.btnWalk.text = "WALK"
            binding.btnWalk.setTextColor(0xFFFFFFFF.toInt())
        }
    }

    private fun fireJump() {
        sendWireAsync(JSONObject().put("c", "jump"), "jump")
    }

    private fun emergencyStop() {
        // Cancel any active planner goal + slam the brakes.
        try { orch.cancelGoal() } catch (_: Throwable) {}
        sendWireAsync(JSONObject().put("c", "stop"), "EMERGENCY STOP")
        walkActive = false
        binding.btnWalk.text = "WALK"
        binding.btnWalk.setTextColor(0xFFFFFFFF.toInt())
    }

    private fun saveSettings() {
        val cfg = orch.config
        cfg.apiKey = binding.editApiKey.text.toString().trim()
        cfg.plannerModel = binding.editPlannerModel.text.toString().trim()
            .ifEmpty { Config.DEFAULT_PLANNER_MODEL }
        cfg.wakeWord = binding.editWakeWord.text.toString().trim()
            .ifEmpty { Config.DEFAULT_WAKE_WORD }
        cfg.wakeRequired = binding.cbWakeRequired.isChecked
        cfg.ttsEnabled = binding.cbTts.isChecked
        Toast.makeText(this, "Settings saved", Toast.LENGTH_SHORT).show()
        RobotState.appendLog("[ui] settings saved")
    }
}

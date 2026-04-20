package dev.robot.companion

import android.Manifest
import android.animation.ObjectAnimator
import android.animation.ValueAnimator
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.view.View
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
    private var bleDotPulse: ObjectAnimator? = null
    private var micPulseAnim: ObjectAnimator? = null
    private var lastShownSay = ""
    private var lastShownGoalState = ""

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
                    // Brief activity flash on every BLE frame (RX = blue).
                    flashActivity(rx = true)
                    // Parse battery voltage if present.
                    try {
                        val j = JSONObject(line)
                        // Firmware state packet: v = voltage*10 (centi-volts / 10)
                        val vCenti = j.optInt("v", -1)
                        if (vCenti > 0) {
                            RobotState.update { it.copy(battV = vCenti / 10f) }
                        }
                        val vFallback = j.optDouble("batt_v", -1.0)
                        if (vFallback > 0) RobotState.update { it.copy(battV = vFallback.toFloat()) }
                        // F2: feed IMU samples into the tilt reflex.  State packets
                        // arrive at 10 Hz with "imu":[ax,ay,az,gx,gy,gz] in g / deg-s.
                        val imu = j.optJSONArray("imu")
                        if (imu != null && imu.length() >= 6) {
                            orch.imuReflex.onImu(
                                imu.optDouble(0, 0.0).toFloat(),
                                imu.optDouble(1, 0.0).toFloat(),
                                imu.optDouble(2, 0.0).toFloat(),
                                imu.optDouble(3, 0.0).toFloat(),
                                imu.optDouble(4, 0.0).toFloat(),
                                imu.optDouble(5, 0.0).toFloat(),
                            )
                        }
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

        // Tab switch (Control <-> Debug)
        binding.btnTabControl.setOnClickListener { showTab(0) }
        binding.btnTabDebug.setOnClickListener { showTab(1) }

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
        binding.cbImuReflex.isChecked = cfg.imuReflexEnabled
        binding.cbImuReflex.setOnCheckedChangeListener { _, checked ->
            cfg.imuReflexEnabled = checked
            orch.imuReflex.setEnabled(checked)
        }

        // Observe RobotState for UI refresh.
        lifecycleScope.launch {
            RobotState.state.collect { snap ->
                renderBleState(snap.bleStatus, snap.bleMac)
                renderBattery(snap.battV)
                renderGoal(snap.goal, snap.goalState)
                renderSay(snap.lastSay)
                renderYouSaid(snap.lastHeard)
                refreshEmptyState(snap)
            }
        }
        lifecycleScope.launch {
            RobotState.log.collect { lines ->
                binding.bleLog.text = lines.takeLast(40).joinToString("\n")
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
            renderMicState(false)
            return
        }
        val v = VoiceListener(this, orch.config, onUtterance = { utt ->
            RobotState.appendLog("[ui] submitting goal: \"$utt\"")
            RobotState.update { it.copy(lastHeard = utt) }
            orch.submitGoal(utt)
        })
        v.start()
        voice = v
        voiceActive = true
        renderMicState(true)
    }

    private fun renderMicState(listening: Boolean) {
        if (listening) {
            binding.btnMic.text = "🔴\nStop"
            binding.btnMic.setBackgroundResource(R.drawable.talk_button_bg_listening)
            binding.micHint.text = "Listening…"
            binding.micHint.setTextColor(ContextCompat.getColor(this, R.color.bad))
            // Pulse halo
            val pulse = binding.micPulse
            micPulseAnim?.cancel()
            pulse.alpha = 0.35f
            pulse.scaleX = 1f
            pulse.scaleY = 1f
            val anim = ObjectAnimator.ofPropertyValuesHolder(
                pulse,
                android.animation.PropertyValuesHolder.ofFloat("scaleX", 1f, 1.25f),
                android.animation.PropertyValuesHolder.ofFloat("scaleY", 1f, 1.25f),
                android.animation.PropertyValuesHolder.ofFloat("alpha", 0.45f, 0f)
            ).apply {
                duration = 1100
                repeatCount = ValueAnimator.INFINITE
                start()
            }
            micPulseAnim = anim
        } else {
            binding.btnMic.text = "🎤\nTalk"
            binding.btnMic.setBackgroundResource(R.drawable.talk_button_bg)
            binding.micHint.text = "Tap to talk"
            binding.micHint.setTextColor(ContextCompat.getColor(this, R.color.fg_dim))
            micPulseAnim?.cancel()
            micPulseAnim = null
            binding.micPulse.alpha = 0f
        }
    }

    private fun showTab(idx: Int) {
        binding.tabFlipper.displayedChild = idx
        if (idx == 0) {
            binding.btnTabControl.setBackgroundResource(R.drawable.tab_bg_active)
            binding.btnTabControl.setTextColor(ContextCompat.getColor(this, R.color.fg))
            binding.btnTabDebug.setBackgroundResource(android.R.color.transparent)
            binding.btnTabDebug.setTextColor(ContextCompat.getColor(this, R.color.fg_dim))
        } else {
            binding.btnTabDebug.setBackgroundResource(R.drawable.tab_bg_active)
            binding.btnTabDebug.setTextColor(ContextCompat.getColor(this, R.color.fg))
            binding.btnTabControl.setBackgroundResource(android.R.color.transparent)
            binding.btnTabControl.setTextColor(ContextCompat.getColor(this, R.color.fg_dim))
        }
    }

    private fun renderSay(say: String) {
        if (say.isEmpty()) {
            binding.bubbleRobotRow.visibility = View.GONE
            binding.saySnapshot.text = ""
            lastShownSay = ""
            return
        }
        binding.saySnapshot.text = say
        if (binding.bubbleRobotRow.visibility != View.VISIBLE || say != lastShownSay) {
            binding.bubbleRobotRow.visibility = View.VISIBLE
            binding.bubbleRobotRow.translationY = 20f
            binding.bubbleRobotRow.alpha = 0f
            binding.bubbleRobotRow.animate()
                .translationY(0f)
                .alpha(1f)
                .setDuration(220)
                .start()
        }
        lastShownSay = say
    }

    private fun renderYouSaid(heard: String) {
        if (heard.isEmpty()) {
            binding.bubbleYouRow.visibility = View.GONE
            return
        }
        binding.bubbleYou.text = heard
        if (binding.bubbleYouRow.visibility != View.VISIBLE) {
            binding.bubbleYouRow.visibility = View.VISIBLE
            binding.bubbleYouRow.translationY = 20f
            binding.bubbleYouRow.alpha = 0f
            binding.bubbleYouRow.animate()
                .translationY(0f)
                .alpha(1f)
                .setDuration(220)
                .start()
        }
    }

    private fun refreshEmptyState(snap: RobotState.Snapshot) {
        val connected = snap.bleStatus.contains("connected", ignoreCase = true)
        val hasAnyActivity = snap.lastSay.isNotEmpty() ||
            snap.lastHeard.isNotEmpty() ||
            (snap.goal.isNotEmpty() && snap.goalState != "idle")
        binding.emptyState.visibility =
            if (connected && !hasAnyActivity) View.VISIBLE else View.GONE
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

    // ---- UI renderers (semantic colors + animation) ------------------

    private fun renderBleState(status: String, mac: String) {
        val (dotDrawable, textColor, label) = when {
            status.contains("connected", ignoreCase = true) ->
                Triple(R.drawable.dot_ok, R.color.ok, "connected")
            status.contains("scan", ignoreCase = true) ||
            status.contains("connect", ignoreCase = true) ->
                Triple(R.drawable.dot_warn, R.color.warn, "connecting…")
            status.contains("idle", ignoreCase = true) ||
            status.isEmpty() ->
                Triple(R.drawable.dot_warn, R.color.fg_dim, "idle")
            else ->
                Triple(R.drawable.dot_bad, R.color.bad, status)
        }
        binding.bleDot.setBackgroundResource(dotDrawable)
        binding.bleStatus.text = label
        binding.bleStatus.setTextColor(ContextCompat.getColor(this, textColor))
        binding.bleMac.text = mac
        startOrStopBlePulse(status.contains("scan", ignoreCase = true) ||
            status.contains("connect", ignoreCase = true) && !status.contains("ed"))
        // On connected, send a quick TX flash to confirm.
        if (status.contains("connected", ignoreCase = true)) flashActivity(rx = false)
    }

    private fun startOrStopBlePulse(connecting: Boolean) {
        if (connecting) {
            if (bleDotPulse?.isRunning == true) return
            bleDotPulse = ObjectAnimator.ofFloat(binding.bleDot, "alpha", 1f, 0.3f, 1f).apply {
                duration = 900
                repeatCount = ValueAnimator.INFINITE
                start()
            }
        } else {
            bleDotPulse?.cancel()
            bleDotPulse = null
            binding.bleDot.alpha = 1f
        }
    }

    private fun renderBattery(v: Float) {
        if (v <= 0f) {
            binding.battStatus.text = "--"
            binding.battFill.layoutParams = binding.battFill.layoutParams.apply { width = 0 }
            return
        }
        // 2S Li-ion window: 6.0 V (empty) -> 8.4 V (full).
        val pct = ((v - 6.0f) / (8.4f - 6.0f)).coerceIn(0f, 1f)
        val color = when {
            pct > 0.5f -> R.color.batt_full
            pct > 0.2f -> R.color.batt_mid
            else       -> R.color.batt_low
        }
        binding.battFill.setBackgroundColor(ContextCompat.getColor(this, color))
        binding.battStatus.text = "%.2f V".format(v)
        // Width update on next layout pass.
        binding.battFill.post {
            val parent = binding.battFill.parent as? View ?: return@post
            val lp = binding.battFill.layoutParams
            lp.width = (parent.width * pct).toInt()
            binding.battFill.layoutParams = lp
        }
    }

    private fun renderGoal(goal: String, state: String) {
        // Hide pill entirely when there is no goal and we're idle.
        val hide = goal.isEmpty() && state.equals("idle", ignoreCase = true)
        if (hide) {
            binding.goalRow.visibility = View.GONE
            lastShownGoalState = ""
            return
        }
        val shown = if (goal.isEmpty()) "—" else goal
        binding.goalStatus.text = "🎯 $shown  · $state"
        val color = when (state.lowercase()) {
            "active", "running", "followup" -> R.color.warn
            "done", "completed"              -> R.color.ok
            "cancelled", "error", "capped"   -> R.color.bad
            else                              -> R.color.accent
        }
        binding.goalStatus.setTextColor(ContextCompat.getColor(this, color))
        // First reveal: slide in.
        if (binding.goalRow.visibility != View.VISIBLE) {
            binding.goalRow.visibility = View.VISIBLE
            binding.goalRow.alpha = 0f
            binding.goalRow.translationX = -30f
            binding.goalRow.animate()
                .alpha(1f)
                .translationX(0f)
                .setDuration(220)
                .start()
        } else if (state != lastShownGoalState) {
            // State transition: small scale bump for feedback.
            binding.goalRow.animate().cancel()
            binding.goalRow.scaleX = 1f; binding.goalRow.scaleY = 1f
            binding.goalRow.animate()
                .scaleX(1.08f).scaleY(1.08f)
                .setDuration(120)
                .withEndAction {
                    binding.goalRow.animate()
                        .scaleX(1f).scaleY(1f)
                        .setDuration(120)
                        .start()
                }
                .start()
        }
        lastShownGoalState = state
    }

    private fun flashActivity(rx: Boolean) {
        val color = if (rx) R.color.pulse_rx else R.color.pulse_tx
        binding.activityFlash.setBackgroundColor(ContextCompat.getColor(this, color))
        binding.activityFlash.animate().cancel()
        binding.activityFlash.alpha = 1f
        binding.activityFlash.animate()
            .alpha(0f)
            .setDuration(400)
            .start()
    }

    private fun sendWireAsync(cmd: JSONObject, label: String) {
        RobotState.appendLog("[remote] $label")
        flashActivity(rx = false)  // green TX pulse
        lifecycleScope.launch {
            try {
                val ack = withContext(Dispatchers.IO) { orch.wire.send(cmd) }
                RobotState.appendLog("[remote]   ack=$ack")
            } catch (e: Throwable) {
                RobotState.appendLog("[remote]   err: ${e.message}")
                Toast.makeText(this@MainActivity,
                    "Wire failed: ${e.message}", Toast.LENGTH_SHORT).show()
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
        cfg.imuReflexEnabled = binding.cbImuReflex.isChecked
        orch.imuReflex.setEnabled(cfg.imuReflexEnabled)
        Toast.makeText(this, "Settings saved", Toast.LENGTH_SHORT).show()
        RobotState.appendLog("[ui] settings saved")
    }
}

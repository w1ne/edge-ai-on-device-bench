package dev.robot.companion

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/**
 * adb-driven smoke test entry point.
 *   adb shell am broadcast -a dev.robot.TEST_GOAL --es goal "look around and tell me if you see a laptop"
 *
 * Delegates to [Orchestrator.submitGoal] so the same code path as the mic
 * button is exercised.
 */
class TestBroadcastReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        when (intent.action) {
            ACTION_TEST_GOAL -> {
                val goal = intent.getStringExtra("goal")?.trim().orEmpty()
                if (goal.isEmpty()) {
                    RobotState.appendLog("[test] TEST_GOAL received with no --es goal")
                    return
                }
                RobotState.appendLog("[test] TEST_GOAL: \"$goal\"")
                Orchestrator.getOrInit(context.applicationContext).submitGoal(goal)
            }
            ACTION_TEST_SAY -> {
                val text = intent.getStringExtra("text")?.trim().orEmpty()
                if (text.isNotEmpty()) {
                    Orchestrator.getOrInit(context.applicationContext).say(text)
                }
            }
            ACTION_SET_API_KEY -> {
                val key = intent.getStringExtra("key")?.trim().orEmpty()
                if (key.isNotEmpty()) {
                    Orchestrator.getOrInit(context.applicationContext).config.apiKey = key
                    RobotState.appendLog("[test] API key set via broadcast (len=${key.length})")
                }
            }
            ACTION_SET_WAKE_REQUIRED -> {
                val on = intent.getBooleanExtra("enabled", false)
                val o = Orchestrator.getOrInit(context.applicationContext)
                o.config.wakeRequired = on
                RobotState.appendLog("[test] wakeRequired=$on")
            }
            ACTION_SET_IDLE_ENABLED -> {
                val on = intent.getBooleanExtra("enabled", false)
                val o = Orchestrator.getOrInit(context.applicationContext)
                o.config.idleLoopEnabled = on
                o.idleLoop.setEnabled(on)
                RobotState.appendLog("[test] idle loop enabled=$on")
            }
            ACTION_TEST_MIC -> {
                // Headless mic smoke: start the SpeechRecognizer loop, log that
                // it's running, stop after a few seconds.  Doesn't need audio.
                val o = Orchestrator.getOrInit(context.applicationContext)
                val vl = VoiceListener(context.applicationContext, o.config,
                    onUtterance = { RobotState.appendLog("[test] heard: $it") })
                vl.start()
                RobotState.appendLog("[test] mic listener started (running=${vl.isRunning()})")
                android.os.Handler(android.os.Looper.getMainLooper()).postDelayed({
                    vl.stop()
                    RobotState.appendLog("[test] mic listener stopped")
                }, 4000)
            }
            ACTION_TEST_IMU_REFLEX -> {
                // F2: inject a synthetic IMU sample to drive the tilt reflex.
                //   adb shell am broadcast -a dev.robot.TEST_IMU_REFLEX \
                //       --ef ax 0.5 --ef ay 0 --ef az 0.85
                // Call ≥3 times at 10 Hz-ish pace to trip the 150 ms debounce.
                val ax = intent.getFloatExtra("ax", 0f)
                val ay = intent.getFloatExtra("ay", 0f)
                val az = intent.getFloatExtra("az", 0.98f)
                val gx = intent.getFloatExtra("gx", 0f)
                val gy = intent.getFloatExtra("gy", 0f)
                val gz = intent.getFloatExtra("gz", 0f)
                val tilt = ImuReflex.tiltDegrees(ax, ay, az)
                RobotState.appendLog(
                    "[test] TEST_IMU_REFLEX ax=$ax ay=$ay az=$az tilt=%.1f°".format(tilt))
                Orchestrator.getOrInit(context.applicationContext)
                    .imuReflex.onImu(ax, ay, az, gx, gy, gz)
            }
        }
    }

    companion object {
        const val ACTION_TEST_GOAL = "dev.robot.TEST_GOAL"
        const val ACTION_TEST_SAY = "dev.robot.TEST_SAY"
        const val ACTION_SET_API_KEY = "dev.robot.SET_API_KEY"
        const val ACTION_TEST_MIC = "dev.robot.TEST_MIC"
        const val ACTION_SET_IDLE_ENABLED = "dev.robot.SET_IDLE_ENABLED"
        const val ACTION_SET_WAKE_REQUIRED = "dev.robot.SET_WAKE_REQUIRED"
        const val ACTION_TEST_IMU_REFLEX = "dev.robot.TEST_IMU_REFLEX"
    }
}

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
        }
    }

    companion object {
        const val ACTION_TEST_GOAL = "dev.robot.TEST_GOAL"
        const val ACTION_TEST_SAY = "dev.robot.TEST_SAY"
        const val ACTION_SET_API_KEY = "dev.robot.SET_API_KEY"
        const val ACTION_TEST_MIC = "dev.robot.TEST_MIC"
    }
}

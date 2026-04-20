package dev.robot.companion

import android.content.Context
import android.content.SharedPreferences

/**
 * SharedPreferences-backed config for DeepInfra API key + planner/vision
 * preferences.  Mirrors the env/~/.dia_key lookup the Python daemon used:
 * if the SharedPrefs slot is empty we look at /sdcard/Download/.dia_key as
 * a fallback so `adb push` still works for first-time setup.
 */
class Config(ctx: Context) {

    private val prefs: SharedPreferences =
        ctx.applicationContext.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)

    companion object {
        private const val PREFS_NAME = "robot_companion"
        private const val K_API_KEY = "deepinfra_api_key"
        private const val K_PLANNER_MODEL = "planner_model"
        private const val K_VISION_MODEL = "vision_model"
        private const val K_WAKE_WORD = "wake_word"
        private const val K_WAKE_REQUIRED = "wake_required"
        private const val K_TTS_ENABLED = "tts_enabled"
        private const val K_IMU_REFLEX = "imu_reflex_enabled"
        private const val K_IDLE_LOOP  = "idle_loop_enabled"

        const val DEFAULT_PLANNER_MODEL = "Qwen/Qwen2.5-72B-Instruct"
        const val DEFAULT_VISION_MODEL = "meta-llama/Llama-3.2-11B-Vision-Instruct"
        const val DEFAULT_WAKE_WORD = "hey robot"

        const val DIA_KEY_SD_PATH = "/sdcard/Download/.dia_key"
    }

    var apiKey: String
        get() {
            val v = prefs.getString(K_API_KEY, "") ?: ""
            if (v.isNotBlank()) return v
            // Fallback: /sdcard/Download/.dia_key — lets `adb push` set the key.
            return try {
                val f = java.io.File(DIA_KEY_SD_PATH)
                if (f.exists()) f.readText().trim() else ""
            } catch (_: Throwable) { "" }
        }
        set(value) { prefs.edit().putString(K_API_KEY, value.trim()).apply() }

    var plannerModel: String
        get() = prefs.getString(K_PLANNER_MODEL, DEFAULT_PLANNER_MODEL) ?: DEFAULT_PLANNER_MODEL
        set(value) { prefs.edit().putString(K_PLANNER_MODEL, value).apply() }

    var visionModel: String
        get() = prefs.getString(K_VISION_MODEL, DEFAULT_VISION_MODEL) ?: DEFAULT_VISION_MODEL
        set(value) { prefs.edit().putString(K_VISION_MODEL, value).apply() }

    var wakeWord: String
        get() = prefs.getString(K_WAKE_WORD, DEFAULT_WAKE_WORD) ?: DEFAULT_WAKE_WORD
        set(value) { prefs.edit().putString(K_WAKE_WORD, value).apply() }

    // Wake-word is default OFF now — the Talk button is explicit "I'm talking
    // to the robot," and requiring "hey robot" before every utterance is
    // friction.  User can re-enable in Debug settings for ambient wake mode.
    var wakeRequired: Boolean
        get() = prefs.getBoolean(K_WAKE_REQUIRED, false)
        set(value) { prefs.edit().putBoolean(K_WAKE_REQUIRED, value).apply() }

    var ttsEnabled: Boolean
        get() = prefs.getBoolean(K_TTS_ENABLED, true)
        set(value) { prefs.edit().putBoolean(K_TTS_ENABLED, value).apply() }

    // F2: IMU tilt reflex — safety feature, defaults ON.
    var imuReflexEnabled: Boolean
        get() = prefs.getBoolean(K_IMU_REFLEX, true)
        set(value) { prefs.edit().putBoolean(K_IMU_REFLEX, value).apply() }

    // F1: Idle autonomous loop — defaults OFF (opt-in).
    var idleLoopEnabled: Boolean
        get() = prefs.getBoolean(K_IDLE_LOOP, false)
        set(value) { prefs.edit().putBoolean(K_IDLE_LOOP, value).apply() }
}

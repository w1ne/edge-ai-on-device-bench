package dev.robot.companion

import android.content.Context
import android.speech.tts.TextToSpeech
import java.util.Locale

/**
 * Tiny wrapper around android.speech.tts.TextToSpeech.  Loads lazily on
 * first say() call; queues utterances until the engine is ready.
 */
class TtsPlayer(private val ctx: Context) {

    private var tts: TextToSpeech? = null
    private var ready = false
    private val pending = ArrayDeque<String>()

    init {
        tts = TextToSpeech(ctx.applicationContext) { status ->
            if (status == TextToSpeech.SUCCESS) {
                tts?.language = Locale.US
                ready = true
                RobotState.appendLog("[tts] engine ready")
                while (pending.isNotEmpty()) speakNow(pending.removeFirst())
            } else {
                RobotState.appendLog("[tts] init failed status=$status")
            }
        }
    }

    fun say(text: String) {
        val t = text.trim()
        if (t.isEmpty()) return
        RobotState.update { it.copy(lastSay = t) }
        RobotState.appendLog("[tts] \"$t\"")
        if (!ready) { pending.addLast(t); return }
        speakNow(t)
    }

    private fun speakNow(text: String) {
        try {
            tts?.speak(text, TextToSpeech.QUEUE_ADD, null, "u${System.nanoTime()}")
        } catch (e: Throwable) {
            RobotState.appendLog("[tts] speak err: ${e.message}")
        }
    }

    fun shutdown() {
        try { tts?.stop(); tts?.shutdown() } catch (_: Throwable) {}
        tts = null
        ready = false
    }
}

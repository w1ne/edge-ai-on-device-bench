package dev.robot.companion

import android.content.Context
import android.content.Intent
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.speech.RecognitionListener
import android.speech.RecognizerIntent
import android.speech.SpeechRecognizer

/**
 * Voice capture via Android SpeechRecognizer.  Hold-to-speak OR
 * tap-to-start-continuous: we auto-restart on end/error so speech keeps
 * flowing until stop() is called.
 *
 * Wake-word filter is applied in-process: partial + final results are
 * checked against [wakeWord] (if [wakeRequired]=true).  If present, the
 * wake word is stripped and the remainder is forwarded via [onUtterance].
 */
class VoiceListener(
    private val ctx: Context,
    private val config: Config,
    private val onUtterance: (String) -> Unit,
    private val logger: (String) -> Unit = { RobotState.appendLog(it) },
) {

    private val main = Handler(Looper.getMainLooper())
    private var recognizer: SpeechRecognizer? = null
    @Volatile private var running = false

    fun isRunning(): Boolean = running

    fun start() {
        if (running) return
        if (!SpeechRecognizer.isRecognitionAvailable(ctx)) {
            logger("[voice] SpeechRecognizer NOT available (needs Google Play Services)")
            return
        }
        running = true
        main.post { startCycle() }
        logger("[voice] listener started (wake='${config.wakeWord}' required=${config.wakeRequired})")
    }

    fun stop() {
        running = false
        main.post {
            try { recognizer?.stopListening() } catch (_: Throwable) {}
            try { recognizer?.destroy() } catch (_: Throwable) {}
            recognizer = null
        }
        logger("[voice] listener stopped")
    }

    private fun startCycle() {
        if (!running) return
        try { recognizer?.destroy() } catch (_: Throwable) {}
        val r = SpeechRecognizer.createSpeechRecognizer(ctx)
        recognizer = r
        r.setRecognitionListener(listener)
        val intent = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH).apply {
            putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL,
                RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
            putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, true)
            putExtra(RecognizerIntent.EXTRA_CALLING_PACKAGE, ctx.packageName)
            putExtra(RecognizerIntent.EXTRA_SPEECH_INPUT_COMPLETE_SILENCE_LENGTH_MILLIS, 1500L)
            putExtra(RecognizerIntent.EXTRA_MAX_RESULTS, 1)
        }
        try {
            r.startListening(intent)
        } catch (e: Throwable) {
            logger("[voice] startListening err: ${e.message}")
            scheduleRestart(1500L)
        }
    }

    private fun scheduleRestart(delayMs: Long) {
        if (!running) return
        main.postDelayed({ if (running) startCycle() }, delayMs)
    }

    private fun applyWake(raw: String): String? {
        val t = raw.trim()
        if (t.isEmpty()) return null
        if (!config.wakeRequired) return t
        val wake = config.wakeWord.trim().lowercase()
        if (wake.isEmpty()) return t
        val lc = t.lowercase()
        val candidates = listOf(wake, "robot", "jarvis")
        for (w in candidates) {
            val idx = lc.indexOf(w)
            if (idx >= 0 && idx < 5) {
                val rest = t.substring(idx + w.length).trimStart(',', '.', ' ', ':', ';').trim()
                if (rest.isNotEmpty()) return rest
            }
        }
        return null
    }

    private val listener = object : RecognitionListener {
        override fun onReadyForSpeech(params: Bundle?) {}
        override fun onBeginningOfSpeech() {}
        override fun onRmsChanged(rmsdB: Float) {}
        override fun onBufferReceived(buffer: ByteArray?) {}
        override fun onEndOfSpeech() {}
        override fun onResults(results: Bundle?) {
            val list = results?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)
            val text = list?.firstOrNull().orEmpty()
            if (text.isNotBlank()) {
                val utt = applyWake(text)
                if (utt != null) {
                    logger("[voice] -> \"$utt\"")
                    try { onUtterance(utt) } catch (e: Throwable) {
                        logger("[voice] callback err: ${e.message}")
                    }
                } else {
                    logger("[voice] ignored (no wake): \"$text\"")
                }
            }
            scheduleRestart(100L)
        }
        override fun onPartialResults(partialResults: Bundle?) {
            // Partials are advisory only; final results drive dispatch.
        }
        override fun onError(error: Int) {
            val name = when (error) {
                SpeechRecognizer.ERROR_NO_MATCH -> "no_match"
                SpeechRecognizer.ERROR_SPEECH_TIMEOUT -> "timeout"
                SpeechRecognizer.ERROR_AUDIO -> "audio"
                SpeechRecognizer.ERROR_NETWORK -> "network"
                SpeechRecognizer.ERROR_NETWORK_TIMEOUT -> "net_timeout"
                SpeechRecognizer.ERROR_CLIENT -> "client"
                SpeechRecognizer.ERROR_INSUFFICIENT_PERMISSIONS -> "perm"
                SpeechRecognizer.ERROR_SERVER -> "server"
                SpeechRecognizer.ERROR_RECOGNIZER_BUSY -> "busy"
                else -> "e$error"
            }
            // Silent restart for common "nothing said" events; log others.
            if (error != SpeechRecognizer.ERROR_NO_MATCH &&
                error != SpeechRecognizer.ERROR_SPEECH_TIMEOUT) {
                logger("[voice] err=$name")
            }
            scheduleRestart(if (error == SpeechRecognizer.ERROR_RECOGNIZER_BUSY) 1000L else 250L)
        }
        override fun onEvent(eventType: Int, params: Bundle?) {}
    }
}

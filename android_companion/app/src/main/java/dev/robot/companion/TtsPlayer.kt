package dev.robot.companion

import android.content.Context
import android.media.AudioAttributes
import android.media.AudioManager
import android.speech.tts.TextToSpeech
import java.util.Locale

/**
 * Tiny wrapper around android.speech.tts.TextToSpeech.  Loads lazily on
 * first say() call; queues utterances until the engine is ready.
 *
 * Explicitly pins output to the MUSIC/MEDIA audio stream so the engine's
 * default routing can't accidentally land on a muted stream (we hit this
 * once: NOTIFICATION stream was muted while MEDIA was unmuted, which made
 * TTS silent depending on device settings).
 */
class TtsPlayer(private val ctx: Context) {

    private var tts: TextToSpeech? = null
    private var ready = false
    private val pending = ArrayDeque<String>()

    init {
        tts = TextToSpeech(ctx.applicationContext) { status ->
            if (status == TextToSpeech.SUCCESS) {
                tts?.language = Locale.US
                tts?.setAudioAttributes(
                    AudioAttributes.Builder()
                        .setUsage(AudioAttributes.USAGE_ASSISTANT)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                        .build()
                )
                pickWarmerVoice()
                // Slightly slower + slightly higher pitch than default for a
                // friendlier, less-robotic feel. Google Assistant uses ~1.05
                // pitch / 1.0 rate; we go a touch warmer.
                tts?.setSpeechRate(0.97f)
                tts?.setPitch(1.08f)
                ready = true
                RobotState.appendLog("[tts] engine ready (stream=MEDIA)")
                warnIfVolumeZero()
                while (pending.isNotEmpty()) speakNow(pending.removeFirst())
            } else {
                RobotState.appendLog("[tts] init failed status=$status")
            }
        }
    }

    /**
     * Google TTS ships several voices of varying quality. By default the
     * engine picks an embedded SMALL-model voice which sounds obviously
     * robotic.  Scan the available set and prefer:
     *    1. en-US network voices (SEANet / WaveNet — most natural)
     *    2. en-US voices with VERY_HIGH / HIGH quality
     *    3. en-GB network voices (fallback)
     * Log which we picked so the user can see what changed.
     */
    private fun pickWarmerVoice() {
        val t = tts ?: return
        try {
            val voices = t.voices ?: return
            val candidates = voices.filter { v ->
                !v.isNetworkConnectionRequired.let { false }  // allow both
                && (v.locale.language == "en")
                && !v.features.contains("notInstalled")
            }
            // Rank: prefer network voices (seanet in name), then higher quality.
            val best = candidates.maxByOrNull { v ->
                var score = v.quality   // VERY_HIGH=500, HIGH=400, NORMAL=300
                val n = v.name.lowercase()
                if (n.contains("network")) score += 200
                if (n.contains("seanet")) score += 150
                if (n.contains("wavenet")) score += 150
                if (v.locale.country == "US") score += 100
                score
            } ?: return
            t.voice = best
            RobotState.appendLog(
                "[tts] voice=${best.name} (q=${best.quality}, " +
                "locale=${best.locale}, network=${best.isNetworkConnectionRequired})"
            )
        } catch (e: Throwable) {
            RobotState.appendLog("[tts] voice pick err: ${e.message}")
        }
    }

    private fun warnIfVolumeZero() {
        try {
            val am = ctx.applicationContext.getSystemService(Context.AUDIO_SERVICE)
                as? AudioManager ?: return
            val v = am.getStreamVolume(AudioManager.STREAM_MUSIC)
            if (v == 0) {
                RobotState.appendLog(
                    "[tts] WARNING media volume = 0 (turn volume up to hear robot)")
            }
        } catch (_: Throwable) {}
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

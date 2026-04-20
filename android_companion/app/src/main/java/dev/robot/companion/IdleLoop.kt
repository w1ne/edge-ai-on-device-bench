package dev.robot.companion

import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.delay
import kotlinx.coroutines.withContext
import org.json.JSONObject

/**
 * Idle autonomous loop. While enabled AND no goal is active, every
 * ~30 seconds the robot glances around via a rotating watchlist of
 * phrases, and announces (TTS) any objects newly seen. Stays silent
 * otherwise. Disables itself on low battery.
 *
 * Roadmap item F1 (docs/REAL_ROBOT_ROADMAP.md): "robot glances around +
 * announces new objects every 30 s idle." The user's literal ask
 * ("not just some dummy") — a cheap heartbeat independent of the
 * planner/DeepInfra pipeline.
 *
 * NEVER fires `walk` — only poses (lean_left/right/neutral) for mild
 * viewpoint shift. Safe if robot is on a table.
 */
class IdleLoop(
    private val vision: () -> VisionQuery?,
    private val tts: TtsPlayer,
    private val wire: WireClient,
    private val getBatteryV: () -> Float,
    private val getGoalState: () -> String,
    private val scope: CoroutineScope,
    private val logger: (String) -> Unit = {
        RobotState.appendLog(it)
    },
) {

    companion object {
        const val INTERVAL_MS = 30_000L
        const val DEDUP_WINDOW_MS = 120_000L
        const val BATT_LOW_V = 6.2f
        const val BATT_OK_V = 6.5f
        const val BATT_ANNOUNCE_COOLDOWN_MS = 300_000L
    }

    private val _enabled = MutableStateFlow(false)
    val enabled: StateFlow<Boolean> = _enabled

    private var loopJob: Job? = null
    private val seen = mutableMapOf<String, Long>()
    private var lowBattAnnouncedAt = 0L
    private var batteryOkStreak = 0

    private val watchlists = listOf(
        listOf("a person", "a laptop", "a chair"),
        listOf("a dog", "a cup", "a phone"),
        listOf("a bottle", "a book", "a plant"),
    )
    private var watchIdx = 0

    fun setEnabled(on: Boolean) {
        if (_enabled.value == on) return
        _enabled.value = on
        if (on) start() else stopLoop()
        logger("[idle] enabled=$on")
    }

    fun start() {
        if (loopJob?.isActive == true) return
        _enabled.value = true
        loopJob = scope.launch(Dispatchers.IO) {
            logger("[idle] loop started (every ${INTERVAL_MS / 1000} s)")
            while (isActive && _enabled.value) {
                try {
                    delay(INTERVAL_MS)
                    tick()
                } catch (e: Throwable) {
                    logger("[idle] tick err: ${e.javaClass.simpleName}: ${e.message}")
                }
            }
            logger("[idle] loop stopped")
        }
    }

    private fun stopLoop() {
        loopJob?.cancel()
        loopJob = null
    }

    private suspend fun tick() {
        if (!_enabled.value) return

        val gs = getGoalState()
        if (gs != "idle") {
            logger("[idle] skip (goal=$gs)")
            return
        }

        // Battery gate. Under threshold -> announce (with cooldown),
        // disable until voltage recovers for 3 consecutive samples.
        val v = getBatteryV()
        if (v > 0f && v < BATT_LOW_V) {
            batteryOkStreak = 0
            val now = System.currentTimeMillis()
            if (now - lowBattAnnouncedAt > BATT_ANNOUNCE_COOLDOWN_MS) {
                tts.say("Battery low, please charge me.")
                lowBattAnnouncedAt = now
                logger("[idle] battery low v=$v; TTS announced")
            }
            // Keep looping so we can detect recovery; but skip vision work.
            return
        }
        if (v >= BATT_OK_V) batteryOkStreak++
        if (v < BATT_OK_V && v >= BATT_LOW_V) batteryOkStreak = 0

        val v2 = vision() ?: run {
            logger("[idle] no vision yet; skip")
            return
        }

        val phrases = watchlists[watchIdx]
        watchIdx = (watchIdx + 1) % watchlists.size

        logger("[idle] glance phrases=$phrases")
        val r: JSONObject = try {
            withContext(Dispatchers.IO) { v2.query(phrases) }
        } catch (e: Throwable) {
            logger("[idle] vision err: ${e.message}"); return
        }

        val seenArr = r.optJSONArray("seen") ?: return
        if (seenArr.length() == 0) return

        val now = System.currentTimeMillis()
        // Drop entries older than dedup window.
        val stale = seen.filterValues { now - it > DEDUP_WINDOW_MS }.keys
        for (k in stale) seen.remove(k)

        val newOnes = mutableListOf<String>()
        for (i in 0 until seenArr.length()) {
            val p = seenArr.optString(i, "")
            if (p.isBlank()) continue
            if (!seen.containsKey(p)) newOnes.add(p)
            seen[p] = now
        }

        if (newOnes.isEmpty()) {
            logger("[idle] nothing new seen")
            return
        }

        // Emit a small viewpoint shift + announce one.
        val pose = if (System.currentTimeMillis() % 2L == 0L) "lean_left" else "lean_right"
        try {
            wire.send(JSONObject().put("c", "pose").put("n", pose).put("d", 400))
        } catch (_: Throwable) {}

        // Announce the first new one (don't stack TTS).
        val spoken = newOnes.first()
        tts.say("I see $spoken.")
        logger("[idle] announced \"$spoken\" (new among ${newOnes.size})")

        // Return to neutral after a beat.
        try {
            delay(900)
            wire.send(JSONObject().put("c", "pose").put("n", "neutral").put("d", 400))
        } catch (_: Throwable) {}
    }
}

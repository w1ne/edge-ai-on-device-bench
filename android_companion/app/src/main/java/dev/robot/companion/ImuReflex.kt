package dev.robot.companion

import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import org.json.JSONObject
import kotlin.math.abs
import kotlin.math.atan2
import kotlin.math.sqrt

/**
 * Safety reflex: when the chassis tilts past 20° for ~150 ms (3 consecutive
 * 10 Hz samples) we fire a `stop` over BLE and speak a warning.  Upside-down
 * (az < -0.5 g) trips on a single sample.
 *
 * Phantom data ([0,0,0,0,0,0]) is ignored with a one-shot warning log — this
 * happens when the MPU-6050 fails to init (current state after the GPIO 8
 * battery-ADC regression clobbered I2C).
 *
 * Hysteresis: once tripped, we stay tripped until tilt stays below 10° for
 * 500 ms.  A 3 s post-trip cooldown prevents one bump from firing repeatedly.
 */
class ImuReflex(
    private val wire: WireClient,
    private val tts: TtsPlayer,
    private val scope: CoroutineScope,
    private val logger: (String) -> Unit = { RobotState.appendLog(it) },
) {
    // Tunables — exposed as public vals so tests / debug UI can poke them.
    var tripDegrees: Float = TRIP_DEGREES_DEFAULT
    var untripDegrees: Float = UNTRIP_DEGREES_DEFAULT
    var tripSamples: Int = TRIP_SAMPLES_DEFAULT
    var untripHoldMs: Long = UNTRIP_HOLD_MS_DEFAULT
    var cooldownMs: Long = COOLDOWN_MS_DEFAULT
    var flipAzThreshold: Float = FLIP_AZ_DEFAULT  // upside-down threshold in g

    @Volatile private var enabled: Boolean = true
    @Volatile private var phantomWarned: Boolean = false

    // Running counters for debounce.
    private var overSamples: Int = 0
    private var underSinceMs: Long = 0L
    private var lastTripMs: Long = 0L

    private val _tripped = MutableStateFlow(false)
    val tripped: StateFlow<Boolean> = _tripped.asStateFlow()

    fun setEnabled(on: Boolean) {
        enabled = on
        // Clear counters so toggling off->on doesn't re-fire from stale state.
        overSamples = 0
        underSinceMs = 0L
        logger("[imu] reflex ${if (on) "enabled" else "disabled"}")
    }

    fun isEnabled(): Boolean = enabled

    /** Feed one IMU sample at 10 Hz.  Call from BLE parser. */
    fun onImu(ax: Float, ay: Float, az: Float, gx: Float, gy: Float, gz: Float) {
        if (!enabled) return

        // Phantom data — MPU-6050 not initialised.  One warning, then silent.
        if (ax == 0f && ay == 0f && az == 0f && gx == 0f && gy == 0f && gz == 0f) {
            if (!phantomWarned) {
                phantomWarned = true
                logger("[imu] WARN phantom zero data — reflex suspended until valid samples arrive")
            }
            return
        }
        // Once we see valid data, allow another phantom warning next time it flips off.
        phantomWarned = false

        val now = System.currentTimeMillis()

        // Upside-down: trip immediately on a single sample.
        if (az < -flipAzThreshold) {
            val tilt = tiltDegrees(ax, ay, az)
            fireTrip(tilt, now, reason = "upside_down az=$az")
            return
        }

        val tilt = tiltDegrees(ax, ay, az)

        if (tilt > tripDegrees) {
            overSamples++
            underSinceMs = 0L
            if (!_tripped.value && overSamples >= tripSamples) {
                fireTrip(tilt, now, reason = "tilt")
            }
        } else {
            overSamples = 0
            if (_tripped.value && tilt < untripDegrees) {
                if (underSinceMs == 0L) underSinceMs = now
                if (now - underSinceMs >= untripHoldMs) {
                    _tripped.value = false
                    underSinceMs = 0L
                    logger("[imu] untrip — tilt back under ${untripDegrees}° for ${untripHoldMs}ms")
                }
            } else {
                underSinceMs = 0L
            }
        }
    }

    private fun fireTrip(tiltDeg: Float, now: Long, reason: String) {
        if (now - lastTripMs < cooldownMs) {
            // Within cooldown — log once but do not re-fire.
            if (!_tripped.value) {
                logger("[imu] trip suppressed (cooldown) tilt=%.1f°".format(tiltDeg))
            }
            return
        }
        lastTripMs = now
        _tripped.value = true
        overSamples = 0
        logger("[imu] TRIP %s tilt=%.1f° — firing stop".format(reason, tiltDeg))
        scope.launch(Dispatchers.IO) {
            try {
                val ack = wire.send(JSONObject().put("c", "stop"))
                logger("[imu]   stop ack=$ack")
            } catch (e: Throwable) {
                logger("[imu]   stop failed: ${e.message}")
            }
        }
        // Speak on any thread — TtsPlayer is internally queued.
        try { tts.say("Whoa!") } catch (_: Throwable) {}
    }

    companion object {
        const val TRIP_DEGREES_DEFAULT: Float = 20f
        const val UNTRIP_DEGREES_DEFAULT: Float = 10f
        const val TRIP_SAMPLES_DEFAULT: Int = 3       // 3 × 100 ms = 150 ms at 10 Hz
        const val UNTRIP_HOLD_MS_DEFAULT: Long = 500L
        const val COOLDOWN_MS_DEFAULT: Long = 3000L
        const val FLIP_AZ_DEFAULT: Float = 0.5f

        /** tilt = atan2(sqrt(ax²+ay²), |az|) converted to degrees. */
        fun tiltDegrees(ax: Float, ay: Float, az: Float): Float {
            val horiz = sqrt((ax * ax + ay * ay).toDouble())
            val rad = atan2(horiz, abs(az).toDouble())
            return Math.toDegrees(rad).toFloat()
        }
    }
}

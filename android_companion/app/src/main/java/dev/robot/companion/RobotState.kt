package dev.robot.companion

import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow

/**
 * Process-wide observable state.  Activity collects from these flows for UI,
 * background service mutates them.
 */
object RobotState {

    data class Snapshot(
        val bleStatus: String = "idle",
        val tcpStatus: String = "stopped",
        val bleMac: String = "",
        val battV: Float = -1f,
        val goal: String = "",
        val goalState: String = "idle",
        val walking: Boolean = false,
        val lastSay: String = "",
        val lastWireAck: String = "",
    )

    private val _state = MutableStateFlow(Snapshot())
    val state: StateFlow<Snapshot> = _state

    private val _log = MutableStateFlow<List<String>>(emptyList())
    val log: StateFlow<List<String>> = _log

    private const val LOG_MAX = 40

    fun update(transform: (Snapshot) -> Snapshot) {
        _state.value = transform(_state.value)
    }

    fun appendLog(line: String) {
        val ts = android.text.format.DateFormat.format("HH:mm:ss", System.currentTimeMillis())
        val entry = "$ts  $line"
        val next = (_log.value + entry).takeLast(LOG_MAX)
        _log.value = next
        android.util.Log.i("RobotState", line)
    }
}

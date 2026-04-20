package dev.robot.companion

import android.content.Context
import androidx.lifecycle.LifecycleOwner
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.SupervisorJob
import org.json.JSONObject
import java.util.concurrent.Executors

/**
 * Process-wide singleton that stitches together Config, WireClient,
 * TtsPlayer, VisionQuery, Planner, GoalKeeper.  MainActivity calls
 * [attachLifecycle] after the user grants camera permission to wire the
 * CameraX capture path.
 *
 * Goals (mic or broadcast) funnel through [submitGoal] which hands off to
 * GoalKeeper.setGoal on a background executor so the caller never blocks.
 */
class Orchestrator private constructor(appCtx: Context) {

    val config = Config(appCtx)
    val wire = WireClient(appCtx)
    val tts = TtsPlayer(appCtx)

    @Volatile var vision: VisionQuery? = null
        private set

    private val bg = Executors.newSingleThreadExecutor()
    private val reflexScope = CoroutineScope(SupervisorJob())

    /** F2 safety reflex — cuts power via BLE stop when chassis tilts past 20°. */
    val imuReflex: ImuReflex = ImuReflex(wire, tts, reflexScope).also {
        it.setEnabled(config.imuReflexEnabled)
    }

    /** F1 autonomous heartbeat — glances around + announces new objects. */
    val idleLoop: IdleLoop = IdleLoop(
        vision = { vision },
        tts = tts,
        wire = wire,
        getBatteryV = { RobotState.state.value.battV },
        getGoalState = { RobotState.state.value.goalState },
        scope = reflexScope,
    ).also { if (config.idleLoopEnabled) it.start() }

    // Built lazily once vision + wire are available.
    @Volatile private var planner: Planner? = null
    @Volatile private var goalKeeper: GoalKeeper? = null

    init {
        wire.bind()
        RobotState.appendLog("[orch] initialized")
    }

    /** Must be called from MainActivity onCreate after camera permission. */
    fun attachLifecycle(ctx: Context, owner: LifecycleOwner) {
        if (vision != null) return
        val v = VisionQuery(ctx, owner, config)
        v.bindCameraBlocking()
        vision = v
        ensurePlanner()
    }

    @Synchronized
    private fun ensurePlanner() {
        if (planner != null) return
        val tools = buildTools()
        planner = Planner(tools, config)
        goalKeeper = GoalKeeper(planner!!)
        RobotState.appendLog("[orch] planner + goal-keeper ready")
    }

    private fun buildTools(): Map<String, (JSONObject) -> JSONObject> {
        val recentSeen = mutableListOf<String>()

        fun wireCmd(cmd: JSONObject): JSONObject = wire.send(cmd)

        return mapOf(
            "pose" to { args ->
                val name = args.optString("name", "neutral")
                val dur = args.optInt("duration_ms", 1500)
                val ack = wireCmd(JSONObject().put("c", "pose").put("n", name).put("d", dur))
                JSONObject().put("ok", ack.optBoolean("ok", true)).put("ack", ack)
            },
            "walk" to { args ->
                val stride = args.optInt("stride", 150)
                val step = args.optInt("step", 400)
                val ack = wireCmd(JSONObject().put("c", "walk").put("on", true)
                    .put("stride", stride).put("step", step))
                RobotState.update { it.copy(walking = true) }
                JSONObject().put("ok", ack.optBoolean("ok", true)).put("ack", ack)
            },
            "stop" to { _ ->
                val ack = wireCmd(JSONObject().put("c", "stop"))
                RobotState.update { it.copy(walking = false) }
                JSONObject().put("ok", ack.optBoolean("ok", true)).put("ack", ack)
            },
            "jump" to { _ ->
                val ack = wireCmd(JSONObject().put("c", "jump"))
                JSONObject().put("ok", ack.optBoolean("ok", true)).put("ack", ack)
            },
            "look" to { args ->
                val dir = args.optString("direction", "ahead")
                JSONObject().put("ok", true).put("direction", dir)
                    .put("seen", org.json.JSONArray(recentSeen))
            },
            "look_for" to { args ->
                val q = args.optString("query", "")
                val v = vision
                if (v == null || q.isBlank()) {
                    JSONObject().put("ok", true).put("seen", false)
                        .put("score", 0.0).put("frame_ms", 0)
                        .put("_source", "no_vision")
                } else {
                    try {
                        val r = v.query(listOf(q))
                        val seenArr = r.optJSONArray("seen") ?: org.json.JSONArray()
                        val scoresObj = r.optJSONObject("scores") ?: JSONObject()
                        var topScore = 0.0
                        val keys = scoresObj.keys()
                        while (keys.hasNext()) {
                            val k = keys.next()
                            val s = scoresObj.optDouble(k, 0.0)
                            if (s > topScore) topScore = s
                        }
                        val seen = seenArr.length() > 0
                        if (seen) {
                            for (i in 0 until seenArr.length()) recentSeen.add(seenArr.optString(i))
                            if (recentSeen.size > 8) recentSeen.removeAt(0)
                        }
                        JSONObject().put("ok", true).put("seen", seen)
                            .put("score", topScore)
                            .put("frame_ms", r.optInt("frame_ms", 0))
                            .put("matches", seenArr)
                    } catch (e: Throwable) {
                        JSONObject().put("ok", false)
                            .put("error", "${e.javaClass.simpleName}: ${e.message}")
                    }
                }
            },
            "say" to { args ->
                val text = args.optString("text", "")
                if (text.isNotBlank()) tts.say(text)
                JSONObject().put("ok", true)
            },
            "wait" to { args ->
                val s = args.optDouble("seconds", 0.0).coerceIn(0.0, 5.0)
                try { Thread.sleep((s * 1000.0).toLong()) } catch (_: Throwable) {}
                JSONObject().put("ok", true)
            }
        )
    }

    fun say(text: String) { tts.say(text) }

    fun submitGoal(goal: String) {
        ensurePlanner()
        val gk = goalKeeper ?: return
        bg.execute {
            try {
                val r = gk.setGoal(goal)
                RobotState.appendLog("[orch] goal done: " + r.toString().take(240))
            } catch (e: Throwable) {
                RobotState.appendLog("[orch] goal err: ${e.javaClass.simpleName}: ${e.message}")
            }
        }
    }

    fun pushVisionEvent(event: JSONObject) {
        goalKeeper?.onEvent(event)
    }

    fun cancelGoal() { goalKeeper?.cancel() }

    companion object {
        @Volatile private var inst: Orchestrator? = null
        fun getOrInit(appCtx: Context): Orchestrator {
            val existing = inst
            if (existing != null) return existing
            synchronized(this) {
                val e2 = inst
                if (e2 != null) return e2
                val o = Orchestrator(appCtx.applicationContext)
                inst = o
                return o
            }
        }
    }
}

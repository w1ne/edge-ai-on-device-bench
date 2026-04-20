package dev.robot.companion

import org.json.JSONObject
import kotlin.concurrent.thread

/**
 * Kotlin port of scripts/termux/phone_goal_keeper.py.  Single-slot
 * standing-goal state machine around [Planner].  States:
 *   idle / active / done / cancelled / capped / error
 */
class GoalKeeper(
    private val planner: Planner,
    private val logger: (String) -> Unit = { RobotState.appendLog(it) },
    private val maxFollowups: Int = 5,
) {

    companion object {
        private val STOPWORDS = setOf(
            "the","a","an","and","or","but","if","then","when","to","for","of","in","on","at","by",
            "with","from","up","down","is","are","was","were","be","been","being","do","does","did",
            "have","has","had","it","its","this","that","these","those","i","you","we","they","he",
            "she","them","his","her","my","your","our","their","me","him","us","here","there","now",
            "tell","say","said","around","about","please","just","let","go","see","look","watch",
            "wait","something","someone","anyone","anything","one","two","back","forward","some",
            "any","all","no","not","out","so","also","will","can","should","could","would","get",
            "got","want"
        )
        private val INERT = setOf("idle", "done", "cancelled", "capped", "error")
        private val TOKEN_RE = Regex("[A-Za-z][A-Za-z0-9_-]*")

        private fun tokens(text: String): Set<String> {
            val out = mutableSetOf<String>()
            for (m in TOKEN_RE.findAll(text.lowercase())) {
                val t = m.value
                if (t.length >= 3 && t !in STOPWORDS) out += t
            }
            return out
        }
    }

    private val lock = Any()

    @Volatile private var goal: String? = null
    @Volatile private var goalTokens: Set<String> = emptySet()
    @Volatile private var state: String = "idle"
    @Volatile private var followups: Int = 0
    @Volatile private var lastResult: JSONObject? = null
    @Volatile private var setAt: Long = 0
    @Volatile private var inflight: Boolean = false
    @Volatile private var version: Int = 0

    fun setGoal(text: String): JSONObject {
        val g = text.trim()
        if (g.isEmpty()) {
            logger("[goal] set_goal('') ignored")
            return JSONObject().put("success", false).put("reason", "empty_goal")
        }
        val prior: String?
        synchronized(lock) {
            prior = goal
            goal = g
            goalTokens = tokens(g)
            state = "active"
            followups = 0
            lastResult = null
            setAt = System.currentTimeMillis()
            version++
        }
        RobotState.update { it.copy(goal = g, goalState = "active") }
        if (prior != null) logger("[goal] replaced prior: '$prior' -> '$g'")
        else logger("[goal] set: '$g'  tokens=${goalTokens.sorted()}")

        val result = try { planner.run(g).toJson() }
        catch (e: Throwable) {
            logger("[goal] initial planner error: ${e.javaClass.simpleName}: ${e.message}")
            val r = JSONObject().put("success", false)
                .put("reason", "error:${e.javaClass.simpleName}")
                .put("final_say", "")
            synchronized(lock) { lastResult = r; state = "error"; version++ }
            RobotState.update { it.copy(goalState = "error") }
            return r
        }
        val reason = result.optString("reason", "")
        val terminal = result.optBoolean("success") &&
            reason.lowercase() !in setOf("", "ok", "watching", "waiting")
        synchronized(lock) {
            lastResult = result
            if (terminal) state = "done"
            version++
        }
        RobotState.update { it.copy(goalState = state) }
        logger("[goal] initial done: success=${result.optBoolean("success")} " +
            "reason='$reason' state=$state")
        return result
    }

    fun cancel() {
        val priorGoal: String?
        synchronized(lock) {
            if (state == "idle") return
            priorGoal = goal
            state = "cancelled"
            version++
        }
        RobotState.update { it.copy(goalState = "cancelled") }
        logger("[goal] cancelled: '$priorGoal'")
    }

    fun onEvent(event: JSONObject) {
        val snap: Triple<String, JSONObject?, Int>
        synchronized(lock) {
            if (state in INERT) return
            val g = goal ?: return
            if (inflight) return
            if (followups >= maxFollowups) {
                logger("[goal] follow-up cap reached")
                state = "capped"
                version++
                RobotState.update { it.copy(goalState = "capped") }
                return
            }
            if (!isRelevant(event)) return
            inflight = true
            followups++
            version++
            snap = Triple(g, lastResult, followups)
        }
        thread(name = "goalkeeper-followup-${snap.third}", isDaemon = true) {
            runFollowup(snap.first, event, snap.second, snap.third)
        }
    }

    fun status(): JSONObject = synchronized(lock) {
        JSONObject()
            .put("goal", goal)
            .put("state", state)
            .put("followups", followups)
            .put("max_followups", maxFollowups)
            .put("last_result", lastResult)
            .put("set_at", setAt)
            .put("version", version)
    }

    fun waitIdle(timeoutMs: Long = 30_000L): Boolean {
        val deadline = System.currentTimeMillis() + timeoutMs
        while (System.currentTimeMillis() < deadline) {
            if (!inflight) return true
            Thread.sleep(50)
        }
        return !inflight
    }

    private fun isRelevant(event: JSONObject): Boolean {
        val etype = event.optString("type", "").lowercase()
        if (etype == "battery" || etype == "imu") return true
        val bits = mutableListOf<String>()
        for (k in listOf("class", "label", "phrase", "query", "text", "description")) {
            when (val v = event.opt(k)) {
                is String -> if (v.isNotEmpty()) bits += v
                is org.json.JSONArray -> for (i in 0 until v.length()) bits += v.opt(i).toString()
                null, org.json.JSONObject.NULL -> {}
                else -> bits += v.toString()
            }
        }
        if (bits.isEmpty()) return false
        val evTokens = tokens(bits.joinToString(" "))
        val overlap = goalTokens intersect evTokens
        if (overlap.isEmpty()) {
            val gLc = (goal ?: "").lowercase()
            return bits.any { it.isNotEmpty() && it.lowercase() in gLc }
        }
        return true
    }

    private fun runFollowup(g: String, event: JSONObject, prior: JSONObject?, fcount: Int) {
        logger("[goal] follow-up #$fcount for '$g' event=$event")
        val result = try {
            val obs = JSONObject().put("event", event)
            if (prior != null) obs.put("prior_result", prior)
            planner.run(g, obs).toJson()
        } catch (e: Throwable) {
            logger("[goal] follow-up planner err: ${e.javaClass.simpleName}: ${e.message}")
            JSONObject().put("success", false)
                .put("reason", "error:${e.javaClass.simpleName}").put("final_say", "")
        }
        val reason = result.optString("reason", "")
        synchronized(lock) {
            lastResult = result
            inflight = false
            val terminal = result.optBoolean("success") &&
                reason.lowercase() !in setOf("", "ok", "watching", "waiting")
            if (terminal && reason.lowercase() != "ignored") state = "done"
            else if (followups >= maxFollowups && state == "active") {
                state = "capped"
                logger("[goal] follow-up cap reached")
            }
            version++
        }
        RobotState.update { it.copy(goalState = state) }
        logger("[goal] follow-up #$fcount done: success=${result.optBoolean("success")} " +
            "reason='$reason' state=$state")
    }
}

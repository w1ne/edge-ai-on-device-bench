package dev.robot.companion

import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.TimeUnit

/**
 * Kotlin port of scripts/termux/phone_planner.py.  LLM tool-calling loop
 * against DeepInfra, 9 tools, same system prompt, same text-format tag
 * recovery (Meta's `<function=NAME>{...}`).
 */
class Planner(
    private val tools: Map<String, (JSONObject) -> JSONObject>,
    private val config: Config,
    private val logger: (String) -> Unit = { RobotState.appendLog(it) },
    private val maxSteps: Int = 10,
) {

    companion object {
        const val DEEPINFRA_CHAT_URL =
            "https://api.deepinfra.com/v1/openai/chat/completions"
        private const val REQUEST_TIMEOUT_S = 20L
        private const val NO_TOOL_CALL_LIMIT = 2
        private val RETRY_BACKOFFS_MS = longArrayOf(500, 1000, 2000)
        private val RETRYABLE_STATUSES = intArrayOf(0, 429, 500, 502, 503, 504)

        private val FUNC_TAG_RE =
            Regex("""<function=([A-Za-z_][A-Za-z0-9_]*)>\s*(\{[^<]*?\})\s*(?:</function>)?""",
                RegexOption.DOT_MATCHES_ALL)

        val TOOL_SCHEMAS: JSONArray = JSONArray().apply {
            put(schema("pose",
                "Command a servo pose.  Use for leaning, bowing, returning to neutral.  " +
                    "`name` must be one of: neutral, lean_left, lean_right, bow_front.",
                JSONObject().apply {
                    put("type", "object")
                    put("properties", JSONObject()
                        .put("name", JSONObject().put("type", "string")
                            .put("enum", JSONArray(listOf(
                                "neutral", "lean_left", "lean_right", "bow_front"))))
                        .put("duration_ms", JSONObject().put("type", "integer")
                            .put("default", 400)))
                    put("required", JSONArray(listOf("name")))
                }))
            put(schema("walk",
                "Start the walking gait.  This tool ONLY starts walking; " +
                    "to halt motion (including walking), call `stop` instead.",
                JSONObject().apply {
                    put("type", "object")
                    put("properties", JSONObject()
                        .put("stride", JSONObject().put("type", "integer").put("default", 150))
                        .put("step", JSONObject().put("type", "integer").put("default", 400)))
                    put("required", JSONArray())
                }))
            put(schema("stop",
                "Halt all motion immediately — use this to stop walking, " +
                    "stop a pose, or as an emergency halt.",
                JSONObject().put("type", "object").put("properties", JSONObject())))
            put(schema("jump", "Perform one jump.",
                JSONObject().put("type", "object").put("properties", JSONObject())))
            put(schema("look",
                "Glance in a direction and return recent vision events.",
                JSONObject().apply {
                    put("type", "object")
                    put("properties", JSONObject().put("direction",
                        JSONObject().put("type", "string").put("enum",
                            JSONArray(listOf("left", "right", "ahead", "down", "up")))))
                    put("required", JSONArray(listOf("direction")))
                }))
            put(schema("look_for",
                "Open-vocabulary visual query.  Runs a single VLM pass on a " +
                    "fresh camera frame and returns whether the phrase was seen.",
                JSONObject().apply {
                    put("type", "object")
                    put("properties", JSONObject().put("query",
                        JSONObject().put("type", "string")))
                    put("required", JSONArray(listOf("query")))
                }))
            put(schema("say", "Speak a short sentence to the user.",
                JSONObject().apply {
                    put("type", "object")
                    put("properties", JSONObject().put("text",
                        JSONObject().put("type", "string")))
                    put("required", JSONArray(listOf("text")))
                }))
            put(schema("wait", "Pause for the given number of seconds.",
                JSONObject().apply {
                    put("type", "object")
                    put("properties", JSONObject().put("seconds",
                        JSONObject().put("type", "number")))
                    put("required", JSONArray(listOf("seconds")))
                }))
            put(schema("finish",
                "Signal that the goal is complete.  ALWAYS call this as the last step.",
                JSONObject().apply {
                    put("type", "object")
                    put("properties", JSONObject().put("reason",
                        JSONObject().put("type", "string")))
                    put("required", JSONArray(listOf("reason")))
                }))
        }

        private fun schema(name: String, description: String, params: JSONObject): JSONObject {
            return JSONObject()
                .put("type", "function")
                .put("function", JSONObject()
                    .put("name", name)
                    .put("description", description)
                    .put("parameters", params))
        }

        const val SYSTEM_PROMPT =
            "You are the planner for a small quadruped robot.  The user gives you a " +
            "goal in plain English.  You accomplish it by calling the provided " +
            "tools, one or a few at a time, reading the tool results, and deciding " +
            "what to do next.\n" +
            "\n" +
            "Rules:\n" +
            "  - Prefer the fewest tool calls that accomplish the goal.\n" +
            "  - If the user asked you to say something, call `say` with that text.\n" +
            "  - For factual or verbal questions with no physical action " +
            "(weather, math, trivia, yes/no questions about the world): " +
            "answer with `say`, then `finish`.  Do NOT call `look` — `look` is " +
            "only for questions about the physical scene around the robot.\n" +
            "  - If the goal involves finding / spotting something physically " +
            "present, use `look` and then `say` what you saw (or didn't).\n" +
            "  - For open-ended visual questions about arbitrary objects " +
            "('do you see a red mug?', 'is there a laptop on the desk?'), use " +
            "`look_for` with the phrase.  For structured class names already " +
            "known to the robot, `look` is cheaper.\n" +
            "  - To halt any motion, call `stop`.  Do not call `walk` with an " +
            "off/disable argument — `walk` only starts walking.\n" +
            "  - Always call `finish` once the goal is done.  Do not keep calling " +
            "tools after finishing.\n" +
            "  - STANDING GOALS: if the goal requires a FUTURE event or ongoing " +
            "observation (phrases like 'wait for', 'when you see', 'watch for', " +
            "'let me know if', 'find <X>', 'tell me if <X> happens', 'greet every " +
            "<X>', 'walk until you see <X>'): do ONE quick check with `look_for` " +
            "or `look`.  If the target is NOT already visible, start the ongoing " +
            "action if one was requested (e.g. `walk` for 'walk until...') and " +
            "then call `finish(reason=\"watching\")` IMMEDIATELY with NO " +
            "preparatory `say` about the eventual event.  Do NOT emit the eventual " +
            "response on the initial turn.  If the target IS already visible on " +
            "the first check, execute the normal response then " +
            "`finish(reason=\"completed\")`.\n" +
            "  - Never invent tools.  Only use the tools provided.\n" +
            "  - Keep `say` utterances short and natural (one sentence)."
    }

    private val http: OkHttpClient = OkHttpClient.Builder()
        .connectTimeout(REQUEST_TIMEOUT_S, TimeUnit.SECONDS)
        .readTimeout(REQUEST_TIMEOUT_S, TimeUnit.SECONDS)
        .writeTimeout(REQUEST_TIMEOUT_S, TimeUnit.SECONDS)
        .build()

    data class Result(
        val success: Boolean,
        val reason: String,
        val steps: List<JSONObject>,
        val finalSay: String,
    ) {
        fun toJson(): JSONObject = JSONObject()
            .put("success", success)
            .put("reason", reason)
            .put("final_say", finalSay)
            .put("steps", JSONArray(steps))
    }

    private data class HttpResp(val status: Int, val body: JSONObject?, val raw: String)

    private fun post(payload: JSONObject, apiKey: String): HttpResp {
        val media = "application/json; charset=utf-8".toMediaType()
        val req = Request.Builder()
            .url(DEEPINFRA_CHAT_URL)
            .header("Authorization", "Bearer $apiKey")
            .header("Content-Type", "application/json")
            .post(payload.toString().toRequestBody(media))
            .build()
        return try {
            http.newCall(req).execute().use { resp ->
                val raw = resp.body?.string() ?: ""
                val body = try { JSONObject(raw) } catch (_: Throwable) { null }
                HttpResp(resp.code, body, raw)
            }
        } catch (e: Throwable) {
            HttpResp(0, null, "NetErr: ${e.javaClass.simpleName}: ${e.message}")
        }
    }

    private fun postWithRetries(payload: JSONObject, apiKey: String): HttpResp {
        var last = HttpResp(0, null, "")
        val schedule = longArrayOf(0L) + RETRY_BACKOFFS_MS
        for ((i, backoff) in schedule.withIndex()) {
            if (backoff > 0L) Thread.sleep(backoff)
            last = post(payload, apiKey)
            if (last.status == 200 && last.body != null) return last
            if (last.status !in RETRYABLE_STATUSES) return last
            logger("[planner] DeepInfra status=${last.status} " +
                    "attempt=${i + 1}/${schedule.size} body=${last.raw.take(160)}")
        }
        return last
    }

    private fun extractTextToolCalls(text: String): JSONArray {
        val out = JSONArray()
        FUNC_TAG_RE.findAll(text).forEachIndexed { i, m ->
            val name = m.groupValues[1]
            val args = m.groupValues[2].trim()
            out.put(JSONObject()
                .put("id", "textcall_$i")
                .put("type", "function")
                .put("function", JSONObject().put("name", name).put("arguments", args)))
        }
        return out
    }

    private fun parseToolArgs(raw: Any?): JSONObject {
        return when (raw) {
            is JSONObject -> raw
            is String -> {
                val s = raw.trim()
                if (s.isEmpty()) JSONObject()
                else try { JSONObject(s) } catch (_: Throwable) { JSONObject() }
            }
            else -> JSONObject()
        }
    }

    fun run(goal: String, observation: JSONObject? = null): Result {
        val apiKey = config.apiKey
        if (apiKey.isBlank()) {
            logger("[planner] no API key — paste via Settings or put in ${Config.DIA_KEY_SD_PATH}")
            return Result(false, "no_api_key", emptyList(), "")
        }

        val messages = JSONArray()
        messages.put(JSONObject().put("role", "system").put("content", SYSTEM_PROMPT))
        messages.put(JSONObject().put("role", "user").put("content", goal.trim()))
        if (observation != null) {
            val ev = observation.opt("event")
            val prior = observation.optJSONObject("prior_result")
            val sb = StringBuilder()
            sb.append("STANDING GOAL (still active): ${goal.trim()}\n")
            sb.append("NEW OBSERVATION: ${ev?.toString() ?: observation.toString()}\n")
            if (prior != null && prior.optString("final_say", "").isNotEmpty()) {
                sb.append("You previously said: \"${prior.optString("final_say")}\"\n")
            }
            sb.append("Decide: does this observation satisfy the goal? " +
                "If yes, take the one appropriate action and then call `finish`. " +
                "If it does not satisfy the goal, call `finish` with reason='ignored'.")
            messages.put(JSONObject().put("role", "user").put("content", sb.toString()))
        }

        val steps = mutableListOf<JSONObject>()
        var finalSay = ""
        var finishReason: String? = null
        var emptyTurns = 0
        var stopCalled = false
        var success = false

        for (stepIdx in 1..maxSteps) {
            val payload = JSONObject()
                .put("model", config.plannerModel)
                .put("messages", messages)
                .put("tools", TOOL_SCHEMAS)
                .put("tool_choice", "auto")
                .put("temperature", 0.0)
                .put("max_tokens", 512)

            val resp = postWithRetries(payload, apiKey)
            if (resp.status == 401 || resp.status == 403) {
                logger("[planner] AUTH FAILURE (${resp.status}): ${resp.raw.take(300)}")
                return Result(false, "auth", steps, finalSay)
            }
            if (resp.status != 200 || resp.body == null) {
                logger("[planner] DeepInfra failed status=${resp.status} body=${resp.raw.take(200)}")
                return Result(false, "http_${resp.status}", steps, finalSay)
            }

            val msg = try {
                resp.body.getJSONArray("choices").getJSONObject(0).getJSONObject("message")
            } catch (e: Throwable) {
                logger("[planner] bad response body: ${resp.raw.take(300)}")
                return Result(false, "bad_response", steps, finalSay)
            }

            var toolCalls = msg.optJSONArray("tool_calls") ?: JSONArray()
            var text = msg.optString("content", "")

            if (toolCalls.length() == 0 && text.isNotEmpty()) {
                val recovered = extractTextToolCalls(text)
                if (recovered.length() > 0) {
                    logger("[planner] recovered ${recovered.length()} text-fmt tool_calls")
                    toolCalls = recovered
                    text = FUNC_TAG_RE.replace(text, "").trim()
                }
            }

            val asstMsg = JSONObject().put("role", "assistant").put("content", text)
            if (toolCalls.length() > 0) asstMsg.put("tool_calls", toolCalls)
            messages.put(asstMsg)

            if (toolCalls.length() == 0) {
                emptyTurns++
                logger("[planner] step=$stepIdx no tool_calls (empty=$emptyTurns) text=${text.take(120)}")
                if (emptyTurns >= NO_TOOL_CALL_LIMIT) {
                    return Result(false, "no_tool_called", steps, finalSay)
                }
                messages.put(JSONObject().put("role", "user").put("content",
                    "Please continue by calling a tool, or call `finish` if the goal is complete."))
                continue
            }
            emptyTurns = 0

            var doneThisStep = false
            for (tci in 0 until toolCalls.length()) {
                val tc = toolCalls.getJSONObject(tci)
                val tcId = tc.optString("id", "call_$stepIdx")
                val fn = tc.optJSONObject("function") ?: JSONObject()
                val name = fn.optString("name", "")
                val args = parseToolArgs(fn.opt("arguments"))

                if (name == "finish") {
                    finishReason = args.optString("reason", "done")
                    val stepRec = JSONObject()
                        .put("step", stepIdx).put("tool", "finish")
                        .put("args", args)
                        .put("result", JSONObject().put("ok", true).put("reason", finishReason))
                    steps += stepRec
                    logger("[planner] step=$stepIdx tool=finish args=$args")
                    success = true
                    messages.put(JSONObject().put("role", "tool").put("tool_call_id", tcId)
                        .put("name", "finish")
                        .put("content", JSONObject().put("ok", true).put("reason", finishReason).toString()))
                    doneThisStep = true
                    break
                }

                val impl = tools[name]
                val result: JSONObject = if (impl == null) {
                    JSONObject().put("ok", false)
                        .put("error", "unknown tool '$name'; valid: ${tools.keys.sorted()}")
                } else {
                    try {
                        impl(args)
                    } catch (e: Throwable) {
                        JSONObject().put("ok", false)
                            .put("error", "${e.javaClass.simpleName}: ${e.message}")
                    }
                }

                steps += JSONObject()
                    .put("step", stepIdx).put("tool", name)
                    .put("args", args).put("result", result)
                logger("[planner] step=$stepIdx tool=$name args=$args result=${result.toString().take(200)}")

                if (name == "say") {
                    val t = args.optString("text", "")
                    if (t.isNotEmpty()) finalSay = t
                }
                if (name == "stop") stopCalled = true

                messages.put(JSONObject().put("role", "tool").put("tool_call_id", tcId)
                    .put("name", name).put("content", result.toString()))
            }
            if (doneThisStep) break
            if (stopCalled) {
                success = true
                finishReason = finishReason ?: "stop_called"
                break
            }
        }

        if (!success && finishReason == null) {
            return Result(false, "max_steps", steps, finalSay)
        }
        return Result(true, finishReason ?: "ok", steps, finalSay)
    }
}

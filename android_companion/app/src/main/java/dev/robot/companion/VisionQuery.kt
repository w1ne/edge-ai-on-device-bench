package dev.robot.companion

import android.content.Context
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.ImageFormat
import android.graphics.Matrix
import android.graphics.Rect
import android.graphics.YuvImage
import android.util.Base64
import androidx.camera.core.CameraSelector
import androidx.camera.core.ImageCapture
import androidx.camera.core.ImageCaptureException
import androidx.camera.core.ImageProxy
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.lifecycle.LifecycleOwner
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.util.concurrent.CompletableFuture
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

/**
 * Kotlin port of scripts/termux/phone_vision.py.  CameraX captures one frame
 * on demand, we JPEG-compress to 512x512 q70, base64, POST to DeepInfra
 * Llama-3.2 Vision, parse the scores JSON.
 *
 * Must be bound to a LifecycleOwner (MainActivity).  Thread-safe for
 * background callers; capture runs on a single executor.
 */
class VisionQuery(
    private val ctx: Context,
    private val owner: LifecycleOwner,
    private val config: Config,
    private val logger: (String) -> Unit = { RobotState.appendLog(it) },
) {

    companion object {
        private const val URL = "https://api.deepinfra.com/v1/openai/chat/completions"
        private const val API_TIMEOUT_S = 25L
        private const val MAX_DIM = 512
        private const val JPEG_QUALITY = 70
    }

    private val http = OkHttpClient.Builder()
        .connectTimeout(API_TIMEOUT_S, TimeUnit.SECONDS)
        .readTimeout(API_TIMEOUT_S, TimeUnit.SECONDS)
        .writeTimeout(API_TIMEOUT_S, TimeUnit.SECONDS)
        .build()

    private val cameraExec = Executors.newSingleThreadExecutor()
    private var imageCapture: ImageCapture? = null
    private var bound = false

    /** Bind CameraX on the main thread (call once after permission granted). */
    fun bindCameraBlocking() {
        if (bound) return
        val main = android.os.Handler(android.os.Looper.getMainLooper())
        val fut = CompletableFuture<Unit>()
        main.post {
            try {
                val providerFut = ProcessCameraProvider.getInstance(ctx)
                providerFut.addListener({
                    try {
                        val provider = providerFut.get()
                        val capture = ImageCapture.Builder()
                            .setCaptureMode(ImageCapture.CAPTURE_MODE_MINIMIZE_LATENCY)
                            .build()
                        provider.unbindAll()
                        provider.bindToLifecycle(
                            owner, CameraSelector.DEFAULT_BACK_CAMERA, capture
                        )
                        imageCapture = capture
                        bound = true
                        logger("[vision] camera bound")
                    } catch (e: Throwable) {
                        logger("[vision] bind failed: ${e.javaClass.simpleName}: ${e.message}")
                    } finally {
                        fut.complete(Unit)
                    }
                }, androidx.core.content.ContextCompat.getMainExecutor(ctx))
            } catch (e: Throwable) {
                logger("[vision] bind err: ${e.message}")
                fut.complete(Unit)
            }
        }
        try { fut.get(5, TimeUnit.SECONDS) } catch (_: Throwable) {}
    }

    /** Capture one frame and return a JPEG-compressed byte array (<= MAX_DIM). */
    private fun captureJpeg(): Pair<ByteArray?, Int> {
        val cap = imageCapture ?: return null to 0
        val t0 = System.currentTimeMillis()
        val fut = CompletableFuture<ByteArray?>()
        cap.takePicture(cameraExec, object : ImageCapture.OnImageCapturedCallback() {
            override fun onCaptureSuccess(image: ImageProxy) {
                try {
                    val bytes = imageProxyToJpegBytes(image)
                    fut.complete(bytes)
                } catch (e: Throwable) {
                    logger("[vision] decode err: ${e.message}")
                    fut.complete(null)
                } finally {
                    image.close()
                }
            }
            override fun onError(exception: ImageCaptureException) {
                logger("[vision] capture err: ${exception.message}")
                fut.complete(null)
            }
        })
        val result = try {
            fut.get(10, TimeUnit.SECONDS)
        } catch (_: Throwable) { null }
        val dt = (System.currentTimeMillis() - t0).toInt()
        return result to dt
    }

    private fun imageProxyToJpegBytes(image: ImageProxy): ByteArray {
        // Most CameraX JPEG captures yield a single plane already JPEG-encoded.
        val planes = image.planes
        val first = planes[0]
        val buffer = first.buffer
        val rawBytes = ByteArray(buffer.remaining())
        buffer.get(rawBytes)

        val bmp: Bitmap? = when (image.format) {
            ImageFormat.JPEG -> BitmapFactory.decodeByteArray(rawBytes, 0, rawBytes.size)
            ImageFormat.YUV_420_888 -> yuvToBitmap(image)
            else -> BitmapFactory.decodeByteArray(rawBytes, 0, rawBytes.size)
        }
        val bitmap = bmp ?: return rawBytes

        // Rotate per EXIF/image info so the VLM doesn't see a sideways frame.
        val rot = image.imageInfo.rotationDegrees
        val rotated = if (rot != 0) {
            val m = Matrix().apply { postRotate(rot.toFloat()) }
            Bitmap.createBitmap(bitmap, 0, 0, bitmap.width, bitmap.height, m, true)
        } else bitmap

        // Downscale to MAX_DIM on the longest side.
        val (w, h) = rotated.width to rotated.height
        val scale = if (maxOf(w, h) > MAX_DIM) MAX_DIM.toFloat() / maxOf(w, h) else 1f
        val scaled = if (scale < 1f) {
            Bitmap.createScaledBitmap(rotated, (w * scale).toInt(), (h * scale).toInt(), true)
        } else rotated

        val out = ByteArrayOutputStream()
        scaled.compress(Bitmap.CompressFormat.JPEG, JPEG_QUALITY, out)
        return out.toByteArray()
    }

    private fun yuvToBitmap(image: ImageProxy): Bitmap? {
        return try {
            val yBuffer = image.planes[0].buffer
            val uBuffer = image.planes[1].buffer
            val vBuffer = image.planes[2].buffer
            val ySize = yBuffer.remaining()
            val uSize = uBuffer.remaining()
            val vSize = vBuffer.remaining()
            val nv21 = ByteArray(ySize + uSize + vSize)
            yBuffer.get(nv21, 0, ySize)
            vBuffer.get(nv21, ySize, vSize)
            uBuffer.get(nv21, ySize + vSize, uSize)
            val yuv = YuvImage(nv21, ImageFormat.NV21, image.width, image.height, null)
            val os = ByteArrayOutputStream()
            yuv.compressToJpeg(Rect(0, 0, image.width, image.height), 85, os)
            BitmapFactory.decodeByteArray(os.toByteArray(), 0, os.size())
        } catch (_: Throwable) { null }
    }

    /** Returns {"seen": [...], "scores": {..}, "frame_ms": N, "error": null|str}. */
    fun query(phrases: List<String>, threshold: Double = 0.20): JSONObject {
        val clean = phrases.map { it.trim() }.filter { it.isNotEmpty() }
        if (clean.isEmpty()) return JSONObject()
            .put("seen", JSONArray()).put("scores", JSONObject())
            .put("frame_ms", 0).put("error", "no_phrases")

        val (jpeg, frameMs) = captureJpeg()
        if (jpeg == null) return JSONObject()
            .put("seen", JSONArray()).put("scores", JSONObject())
            .put("frame_ms", frameMs).put("error", "cam_failed")

        val apiKey = config.apiKey
        if (apiKey.isBlank()) return JSONObject()
            .put("seen", JSONArray()).put("scores", JSONObject())
            .put("frame_ms", frameMs).put("error", "no_api_key")

        val b64 = Base64.encodeToString(jpeg, Base64.NO_WRAP)
        val prompt = "You are a visual classifier. Look at the attached image. " +
            "For EACH phrase in the list, output the probability in [0,1] " +
            "that the phrase is clearly visible in the image. " +
            "Output ONLY a single JSON object mapping each phrase verbatim " +
            "to its probability. No prose, no markdown, no extra keys.\n" +
            "Phrases: ${JSONArray(clean)}"

        val content = JSONArray()
            .put(JSONObject().put("type", "text").put("text", prompt))
            .put(JSONObject().put("type", "image_url")
                .put("image_url", JSONObject()
                    .put("url", "data:image/jpeg;base64,$b64")))

        val payload = JSONObject()
            .put("model", config.visionModel)
            .put("messages", JSONArray().put(JSONObject()
                .put("role", "user").put("content", content)))
            .put("max_tokens", 256)
            .put("temperature", 0.0)
            .put("response_format", JSONObject().put("type", "json_object"))

        val media = "application/json; charset=utf-8".toMediaType()
        val req = Request.Builder()
            .url(URL)
            .header("Authorization", "Bearer $apiKey")
            .header("Content-Type", "application/json")
            .post(payload.toString().toRequestBody(media))
            .build()

        val tApi = System.currentTimeMillis()
        val raw: String = try {
            http.newCall(req).execute().use { r ->
                val body = r.body?.string() ?: ""
                if (r.code != 200) {
                    logger("[vision] HTTP ${r.code}: ${body.take(200)}")
                    return JSONObject()
                        .put("seen", JSONArray()).put("scores", JSONObject())
                        .put("frame_ms", frameMs)
                        .put("error", if (r.code == 401 || r.code == 403)
                            "api_auth" else "api_http_${r.code}")
                }
                body
            }
        } catch (e: Throwable) {
            logger("[vision] net err: ${e.javaClass.simpleName}: ${e.message}")
            return JSONObject()
                .put("seen", JSONArray()).put("scores", JSONObject())
                .put("frame_ms", frameMs).put("error", "api_network")
        }
        val apiMs = (System.currentTimeMillis() - tApi).toInt()

        val text: String = try {
            JSONObject(raw).getJSONArray("choices").getJSONObject(0)
                .getJSONObject("message").optString("content", "")
        } catch (e: Throwable) {
            logger("[vision] bad body: ${raw.take(200)}")
            return JSONObject()
                .put("seen", JSONArray()).put("scores", JSONObject())
                .put("frame_ms", frameMs).put("error", "api_bad_body")
        }

        val scores = extractScores(text, clean)
        for (p in clean) if (!scores.has(p)) scores.put(p, 0.0)
        val seen = clean.filter { scores.optDouble(it, 0.0) >= threshold }
            .sortedByDescending { scores.optDouble(it, 0.0) }
        logger("[vision] cam=${frameMs}ms api=${apiMs}ms seen=$seen scores=$scores")
        return JSONObject()
            .put("seen", JSONArray(seen)).put("scores", scores)
            .put("frame_ms", frameMs).put("error", JSONObject.NULL)
    }

    private fun extractScores(raw: String, phrases: List<String>): JSONObject {
        var t = raw.trim()
        if (t.startsWith("```")) {
            t = t.removePrefix("```").let { if (it.startsWith("json")) it.removePrefix("json") else it }
            t = t.trim().removeSuffix("```").trim()
        }
        // Try direct JSON parse first.
        val candidates = mutableListOf(t)
        // Fallback: pull first balanced object.
        val first = t.indexOf('{')
        if (first >= 0) {
            var depth = 0; var inStr = false; var esc = false
            for (j in first until t.length) {
                val ch = t[j]
                if (inStr) {
                    if (esc) esc = false
                    else if (ch == '\\') esc = true
                    else if (ch == '"') inStr = false
                } else {
                    if (ch == '"') inStr = true
                    else if (ch == '{') depth++
                    else if (ch == '}') { depth--; if (depth == 0) { candidates += t.substring(first, j + 1); break } }
                }
            }
        }
        for (c in candidates) {
            try {
                val obj = JSONObject(c)
                val target = if (obj.has("scores")) obj.optJSONObject("scores") ?: obj else obj
                val out = JSONObject()
                for (p in phrases) {
                    var v: Any? = target.opt(p)
                    if (v == null || v == JSONObject.NULL) {
                        val keys = target.keys()
                        while (keys.hasNext()) {
                            val k = keys.next()
                            if (k.trim().equals(p.trim(), ignoreCase = true)) {
                                v = target.opt(k); break
                            }
                        }
                    }
                    when (v) {
                        is Boolean -> out.put(p, if (v) 1.0 else 0.0)
                        is Number -> out.put(p, v.toDouble().coerceIn(0.0, 1.0))
                        is String -> {
                            val s = v.trim().lowercase()
                            when (s) {
                                "yes", "true", "y" -> out.put(p, 1.0)
                                "no", "false", "n" -> out.put(p, 0.0)
                                else -> s.toDoubleOrNull()?.let { out.put(p, it.coerceIn(0.0, 1.0)) }
                            }
                        }
                        else -> {}
                    }
                }
                if (out.length() > 0) return out
            } catch (_: Throwable) {}
        }
        // Last-resort regex scan.
        val out = JSONObject()
        for (p in phrases) {
            val re = Regex("\"${Regex.escape(p)}\"\\s*:\\s*([0-9]*\\.?[0-9]+)")
            re.find(raw)?.groupValues?.get(1)?.toDoubleOrNull()?.let {
                out.put(p, it.coerceIn(0.0, 1.0))
            }
        }
        return out
    }

    fun shutdown() {
        try { cameraExec.shutdownNow() } catch (_: Throwable) {}
    }
}

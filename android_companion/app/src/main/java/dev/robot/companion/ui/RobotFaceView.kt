package dev.robot.companion.ui

import android.animation.ValueAnimator
import android.content.Context
import android.graphics.Canvas
import android.graphics.Paint
import android.graphics.Path
import android.graphics.RectF
import android.util.AttributeSet
import android.view.View
import kotlin.math.max
import kotlin.math.min
import kotlin.math.sin

/**
 * Animated robot face rendered entirely in Canvas. Tiny, no assets, state-
 * driven.  Idle: blinks every 3-5 s + gentle breathing scale.  Listening:
 * big round eyes + animated wave mouth.  Talking: mouth opens in sync with
 * a fake phoneme LFO.  Sleeping: closed eyes + "Z Z Z".  Tripped: × eyes +
 * "!" mouth.  Size: ~180 LOC.  Driver: a single ValueAnimator on the
 * attached window.
 */
class RobotFaceView @JvmOverloads constructor(
    ctx: Context, attrs: AttributeSet? = null, defStyle: Int = 0
) : View(ctx, attrs, defStyle) {

    enum class State { IDLE, LISTENING, TALKING, SLEEPING, TRIPPED }

    private val bodyPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply { style = Paint.Style.FILL }
    private val eyePaint = Paint(Paint.ANTI_ALIAS_FLAG).apply { style = Paint.Style.FILL }
    private val accentPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE; strokeCap = Paint.Cap.ROUND
    }
    private val textPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        textAlign = Paint.Align.CENTER; isFakeBoldText = true
    }

    private var state: State = State.IDLE
    private var phase: Float = 0f          // 0..1, advances every frame
    private var blinkPhase: Float = 1f     // 1 = eyes open, 0 = closed
    private var nextBlinkAtMs: Long = 0L
    private var startedAt: Long = System.currentTimeMillis()

    private val driver = ValueAnimator.ofFloat(0f, 1f).apply {
        duration = 2000L
        repeatCount = ValueAnimator.INFINITE
        interpolator = null
        addUpdateListener {
            phase = it.animatedValue as Float
            advanceBlink()
            invalidate()
        }
    }

    // Visual palette — dark UI, soft cyan accent for "alive" state
    private var bodyColor: Int = 0xFF242432.toInt()
    private var eyeColor: Int = 0xFFECECF0.toInt()
    private var accentColor: Int = 0xFF4CAF50.toInt()

    fun setPalette(body: Int, eye: Int, accent: Int) {
        bodyColor = body; eyeColor = eye; accentColor = accent
        invalidate()
    }

    fun setState(s: State) {
        if (s == state) return
        state = s
        invalidate()
    }

    override fun onAttachedToWindow() {
        super.onAttachedToWindow()
        nextBlinkAtMs = System.currentTimeMillis() + 2500L
        driver.start()
    }

    override fun onDetachedFromWindow() {
        super.onDetachedFromWindow()
        driver.cancel()
    }

    private fun advanceBlink() {
        if (state == State.SLEEPING) { blinkPhase = 0f; return }
        if (state == State.TRIPPED)  { blinkPhase = 1f; return }
        val now = System.currentTimeMillis()
        // Each scheduled blink takes 180ms closing + 180ms opening.
        val t = now - nextBlinkAtMs
        blinkPhase = when {
            t < 0L   -> 1f
            t < 180L -> 1f - (t / 180f)
            t < 360L -> (t - 180L) / 180f
            else     -> {
                // Schedule next blink 2.5–5 s out.
                nextBlinkAtMs = now + (2500L + (Math.random() * 2500L).toLong())
                1f
            }
        }
    }

    override fun onDraw(canvas: Canvas) {
        val w = width.toFloat(); val h = height.toFloat()
        if (w <= 0f || h <= 0f) return
        val cx = w / 2f; val cy = h / 2f
        val r  = min(w, h) * 0.42f

        // Breathing scale for idle / talking
        val breath = when (state) {
            State.IDLE      -> 1f + 0.02f * sin(phase * 2 * Math.PI).toFloat()
            State.LISTENING -> 1f + 0.04f * sin(phase * 4 * Math.PI).toFloat()
            State.TALKING   -> 1f + 0.03f * sin(phase * 8 * Math.PI).toFloat()
            else            -> 1f
        }
        val rr = r * breath

        // Body: rounded square head
        bodyPaint.color = bodyColor
        val headPath = Path().apply {
            addRoundRect(cx - rr, cy - rr, cx + rr, cy + rr, rr * 0.28f, rr * 0.28f, Path.Direction.CW)
        }
        canvas.drawPath(headPath, bodyPaint)

        // Antenna
        accentPaint.color = accentColor
        accentPaint.strokeWidth = max(3f, rr * 0.05f)
        canvas.drawLine(cx, cy - rr, cx, cy - rr - rr * 0.22f, accentPaint)
        val antennaY = cy - rr - rr * 0.22f
        canvas.drawCircle(cx, antennaY, rr * 0.07f, accentPaint.apply { style = Paint.Style.FILL })
        accentPaint.style = Paint.Style.STROKE

        // Eyes
        val eyeY = cy - rr * 0.15f
        val eyeDx = rr * 0.38f
        val baseEyeR = rr * 0.16f
        val eyeR = when (state) {
            State.LISTENING -> baseEyeR * 1.25f
            else            -> baseEyeR
        }
        eyePaint.color = eyeColor

        when (state) {
            State.SLEEPING -> {
                accentPaint.color = eyeColor
                accentPaint.strokeWidth = max(4f, rr * 0.06f)
                canvas.drawLine(cx - eyeDx - eyeR, eyeY, cx - eyeDx + eyeR, eyeY, accentPaint)
                canvas.drawLine(cx + eyeDx - eyeR, eyeY, cx + eyeDx + eyeR, eyeY, accentPaint)
                // zzz
                textPaint.color = eyeColor
                textPaint.textSize = rr * 0.22f
                canvas.drawText("z z z", cx + rr * 0.4f, cy - rr * 0.6f, textPaint)
            }
            State.TRIPPED -> {
                accentPaint.color = 0xFFEF5350.toInt()
                accentPaint.strokeWidth = max(4f, rr * 0.08f)
                drawX(canvas, cx - eyeDx, eyeY, eyeR, accentPaint)
                drawX(canvas, cx + eyeDx, eyeY, eyeR, accentPaint)
            }
            else -> {
                // Blinking eyelid: scale Y of eye oval by blinkPhase
                val ey = blinkPhase.coerceIn(0.08f, 1f)
                val rect = RectF()
                rect.set(cx - eyeDx - eyeR, eyeY - eyeR * ey, cx - eyeDx + eyeR, eyeY + eyeR * ey)
                canvas.drawOval(rect, eyePaint)
                rect.set(cx + eyeDx - eyeR, eyeY - eyeR * ey, cx + eyeDx + eyeR, eyeY + eyeR * ey)
                canvas.drawOval(rect, eyePaint)
                if (state == State.LISTENING || state == State.IDLE) {
                    // glossy pupil highlight
                    val hp = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = 0x66FFFFFF }
                    canvas.drawCircle(cx - eyeDx + eyeR * 0.3f, eyeY - eyeR * 0.3f, eyeR * 0.22f, hp)
                    canvas.drawCircle(cx + eyeDx + eyeR * 0.3f, eyeY - eyeR * 0.3f, eyeR * 0.22f, hp)
                }
            }
        }

        // Mouth
        val mouthY = cy + rr * 0.38f
        val mouthW = rr * 0.5f
        accentPaint.color = accentColor
        accentPaint.strokeWidth = max(4f, rr * 0.06f)

        when (state) {
            State.SLEEPING -> {
                // Small flat line
                canvas.drawLine(cx - mouthW * 0.4f, mouthY, cx + mouthW * 0.4f, mouthY, accentPaint)
            }
            State.TRIPPED -> {
                // "!" - a drop + dot
                textPaint.color = 0xFFEF5350.toInt()
                textPaint.textSize = rr * 0.5f
                canvas.drawText("!", cx, mouthY + rr * 0.18f, textPaint)
            }
            State.TALKING -> {
                val open = (0.4f + 0.6f * (sin(phase * 12 * Math.PI).toFloat() + 1f) * 0.5f)
                    .coerceIn(0.2f, 1f)
                val h2 = rr * 0.18f * open
                val rect = RectF(cx - mouthW * 0.45f, mouthY - h2, cx + mouthW * 0.45f, mouthY + h2)
                canvas.drawRoundRect(rect, h2, h2, Paint(Paint.ANTI_ALIAS_FLAG).apply {
                    color = accentColor; style = Paint.Style.FILL
                })
            }
            State.LISTENING -> {
                // Mini waveform mouth: 5 vertical bars inside the mouth area
                val bars = 7
                val step = mouthW * 2 / (bars + 1)
                for (i in 0 until bars) {
                    val t = phase * 6 * Math.PI + i * 0.9
                    val hNorm = (0.3f + 0.7f * (sin(t).toFloat() + 1f) * 0.5f)
                    val bh = rr * 0.22f * hNorm
                    val x = cx - mouthW + step * (i + 1)
                    canvas.drawLine(x, mouthY - bh, x, mouthY + bh, accentPaint)
                }
            }
            State.IDLE -> {
                // Gentle smile arc
                val rect = RectF(cx - mouthW * 0.6f, mouthY - rr * 0.12f,
                                 cx + mouthW * 0.6f, mouthY + rr * 0.22f)
                canvas.drawArc(rect, 20f, 140f, false, accentPaint)
            }
        }
    }

    private fun drawX(c: Canvas, cx: Float, cy: Float, r: Float, p: Paint) {
        c.drawLine(cx - r, cy - r, cx + r, cy + r, p)
        c.drawLine(cx + r, cy - r, cx - r, cy + r, p)
    }
}

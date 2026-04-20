package dev.robot.companion.ui

import android.animation.ValueAnimator
import android.content.Context
import android.graphics.Canvas
import android.graphics.Paint
import android.util.AttributeSet
import android.view.View
import kotlin.math.cos
import kotlin.math.min
import kotlin.math.sin

/**
 * 16-bar radial audio waveform ring around the Talk button.  Runs only when
 * active (15 Hz), idle state is a no-op.  No mic input — heights are
 * pseudo-random LFOs so the user sees reactivity without extra plumbing.
 */
class WaveformRingView @JvmOverloads constructor(
    ctx: Context, attrs: AttributeSet? = null, defStyle: Int = 0
) : View(ctx, attrs, defStyle) {

    private val paint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        strokeCap = Paint.Cap.ROUND
    }
    private val bars = 16
    private val phases = FloatArray(bars) { (Math.random() * Math.PI * 2).toFloat() }
    private val speeds = FloatArray(bars) { 0.6f + Math.random().toFloat() * 1.4f }
    private var t: Float = 0f
    private var active: Boolean = false
    private var color: Int = 0xFFEF5350.toInt()

    private val driver = ValueAnimator.ofFloat(0f, 1f).apply {
        duration = 2000L
        repeatCount = ValueAnimator.INFINITE
        interpolator = null
        addUpdateListener {
            t = (t + 0.06f) % 1e6f
            invalidate()
        }
    }

    fun setActive(on: Boolean) {
        if (on == active) return
        active = on
        if (on) {
            alpha = 1f
            if (!driver.isRunning) driver.start()
        } else {
            driver.cancel()
            alpha = 0f
        }
        invalidate()
    }

    fun setTint(c: Int) { color = c; paint.color = c; invalidate() }

    override fun onDetachedFromWindow() {
        super.onDetachedFromWindow()
        driver.cancel()
    }

    override fun onDraw(canvas: Canvas) {
        if (!active) return
        val w = width.toFloat(); val h = height.toFloat()
        val cx = w / 2f; val cy = h / 2f
        val baseR = min(w, h) * 0.40f
        paint.color = color
        paint.strokeWidth = min(w, h) * 0.018f
        for (i in 0 until bars) {
            val ang = (i / bars.toFloat()) * 2 * Math.PI
            val osc = sin(t * speeds[i] + phases[i]).toFloat()
            val amp = (0.2f + 0.8f * (osc + 1f) * 0.5f)
            val r1 = baseR
            val r2 = baseR + min(w, h) * 0.075f * amp
            val sx = cx + cos(ang).toFloat() * r1
            val sy = cy + sin(ang).toFloat() * r1
            val ex = cx + cos(ang).toFloat() * r2
            val ey = cy + sin(ang).toFloat() * r2
            canvas.drawLine(sx, sy, ex, ey, paint)
        }
    }
}

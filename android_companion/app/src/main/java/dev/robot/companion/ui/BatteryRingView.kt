package dev.robot.companion.ui

import android.content.Context
import android.graphics.Canvas
import android.graphics.Paint
import android.graphics.RectF
import android.util.AttributeSet
import android.view.View
import kotlin.math.min

/**
 * Circular battery gauge — arc from top, sweeps clockwise as charge drops.
 * Color shifts green -> amber -> red by percentage.
 */
class BatteryRingView @JvmOverloads constructor(
    ctx: Context, attrs: AttributeSet? = null, defStyle: Int = 0
) : View(ctx, attrs, defStyle) {

    private val trackPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE; strokeCap = Paint.Cap.ROUND
        color = 0x332A2A33
    }
    private val arcPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE; strokeCap = Paint.Cap.ROUND
    }
    private var pct: Float = 0f
    private val rect = RectF()

    fun setPercent(p: Float) {
        pct = p.coerceIn(0f, 1f); invalidate()
    }

    override fun onDraw(canvas: Canvas) {
        val w = width.toFloat(); val h = height.toFloat()
        val sw = min(w, h) * 0.12f
        arcPaint.strokeWidth = sw
        trackPaint.strokeWidth = sw
        val pad = sw / 2 + 2f
        rect.set(pad, pad, w - pad, h - pad)
        canvas.drawArc(rect, 135f, 270f, false, trackPaint)
        val color = when {
            pct > 0.5f -> 0xFF4CAF50.toInt()
            pct > 0.2f -> 0xFFFFC107.toInt()
            else       -> 0xFFEF5350.toInt()
        }
        arcPaint.color = color
        canvas.drawArc(rect, 135f, 270f * pct, false, arcPaint)
    }
}

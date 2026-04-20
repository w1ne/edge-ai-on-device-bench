package dev.robot.companion.ui

import android.content.Context
import android.graphics.Canvas
import android.graphics.Paint
import android.graphics.RectF
import android.util.AttributeSet
import android.view.View

/**
 * 4-bar BLE signal icon.  [setBars] takes 0..4.  Filled bars are accent
 * color; unfilled are dim.
 */
class SignalBarsView @JvmOverloads constructor(
    ctx: Context, attrs: AttributeSet? = null, defStyle: Int = 0
) : View(ctx, attrs, defStyle) {

    private val p = Paint(Paint.ANTI_ALIAS_FLAG)
    private var filled: Int = 0
    private var accent: Int = 0xFF4CAF50.toInt()
    private var dim: Int = 0x332A2A33

    fun setBars(b: Int) { filled = b.coerceIn(0, 4); invalidate() }
    fun setColors(a: Int, d: Int) { accent = a; dim = d; invalidate() }

    override fun onDraw(canvas: Canvas) {
        val w = width.toFloat(); val h = height.toFloat()
        val barW = w / 5f
        val gap = barW * 0.25f
        val bw = barW - gap
        for (i in 0 until 4) {
            val bh = h * (0.35f + 0.2f * i)
            val x = i * barW + gap
            val y = h - bh
            p.color = if (i < filled) accent else dim
            canvas.drawRoundRect(RectF(x, y, x + bw, h), bw * 0.3f, bw * 0.3f, p)
        }
    }
}

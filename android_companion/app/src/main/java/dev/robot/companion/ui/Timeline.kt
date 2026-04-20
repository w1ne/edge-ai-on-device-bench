package dev.robot.companion.ui

import android.content.Context
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.widget.LinearLayout
import android.widget.TextView
import dev.robot.companion.R

/**
 * Lightweight in-memory activity timeline for the Control tab.  Keeps the
 * 8 most recent events; each item slides+fades in on arrival.  Relative
 * timestamps (5s ago / 2m ago) are updated by [refresh], called on a
 * 5 s tick from the Activity.
 */
class Timeline(private val container: LinearLayout) {

    enum class Kind { GOAL, TILT, BATTERY, VISION, VOICE, INFO, ERROR }

    data class Event(val kind: Kind, val text: String, val tMs: Long)

    private val events = ArrayDeque<Event>()
    private val maxItems = 8
    private val ctx: Context = container.context

    fun push(kind: Kind, text: String) {
        val ev = Event(kind, text, System.currentTimeMillis())
        events.addFirst(ev)
        while (events.size > maxItems) events.removeLast()
        rebuild()
    }

    /** Update relative-time labels without animation. */
    fun refresh() { rebuild(animate = false) }

    private fun rebuild(animate: Boolean = true) {
        val newCount = events.size
        val existing = container.childCount
        // Fast path: just update text for matching rows
        if (existing == newCount) {
            for (i in 0 until newCount) bindRow(container.getChildAt(i) as LinearLayout, events[i])
            return
        }
        container.removeAllViews()
        events.forEachIndexed { i, ev ->
            val row = makeRow(ev)
            container.addView(row)
            if (animate && i == 0) {
                row.translationY = -20f
                row.alpha = 0f
                row.animate().translationY(0f).alpha(1f).setDuration(220).start()
            }
        }
    }

    private fun makeRow(ev: Event): LinearLayout {
        val row = LinearLayout(ctx).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            setPadding(dp(12), dp(9), dp(12), dp(9))
            setBackgroundResource(R.drawable.timeline_item_bg)
            layoutParams = LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT
            ).apply { topMargin = dp(6) }
        }
        bindRow(row, ev)
        return row
    }

    private fun bindRow(row: LinearLayout, ev: Event) {
        row.removeAllViews()
        val (glyph, color) = glyphFor(ev.kind)
        row.addView(TextView(ctx).apply {
            text = glyph
            textSize = 17f
            setTextColor(color)
            layoutParams = LinearLayout.LayoutParams(dp(28), ViewGroup.LayoutParams.WRAP_CONTENT)
        })
        row.addView(TextView(ctx).apply {
            text = ev.text
            textSize = 13f
            setTextColor(0xFFECECF0.toInt())
            layoutParams = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f)
        })
        row.addView(TextView(ctx).apply {
            text = relTime(ev.tMs)
            textSize = 11f
            setTextColor(0xFF8B8B94.toInt())
            layoutParams = LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT)
        })
    }

    private fun relTime(then: Long): String {
        val diff = ((System.currentTimeMillis() - then) / 1000L).coerceAtLeast(0L)
        return when {
            diff < 5L      -> "just now"
            diff < 60L     -> "${diff}s ago"
            diff < 3600L   -> "${diff / 60L}m ago"
            diff < 86_400L -> "${diff / 3600L}h ago"
            else           -> "${diff / 86_400L}d ago"
        }
    }

    private fun glyphFor(k: Kind): Pair<String, Int> = when (k) {
        Kind.GOAL    -> "\uD83C\uDFAF" to 0xFFFFC107.toInt()   // 🎯
        Kind.TILT    -> "\u26A0\uFE0F"  to 0xFFEF5350.toInt()  // ⚠️
        Kind.BATTERY -> "\uD83D\uDD0B" to 0xFF4CAF50.toInt()   // 🔋
        Kind.VISION  -> "\uD83D\uDC41" to 0xFF64B5F6.toInt()   // 👁
        Kind.VOICE   -> "\uD83D\uDCAC" to 0xFF4CAF50.toInt()   // 💬
        Kind.INFO    -> "\u2139\uFE0F"  to 0xFF8B8B94.toInt()  // ℹ️
        Kind.ERROR   -> "\u274C"        to 0xFFEF5350.toInt()  // ❌
    }

    private fun dp(v: Int): Int = (v * ctx.resources.displayMetrics.density + 0.5f).toInt()
}

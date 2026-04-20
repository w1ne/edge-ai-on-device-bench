package dev.robot.companion

import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.ServiceConnection
import android.os.IBinder
import org.json.JSONObject
import java.util.concurrent.CompletableFuture
import java.util.concurrent.TimeUnit

/**
 * In-process fast-path to [BleRobotService].  Binds once, lets the planner
 * tools call `send(cmd)` and get a best-effort ack back.
 *
 * The wire protocol over BLE is fire-and-forget: the robot may or may not
 * answer on a given command.  We treat the first line received within 500 ms
 * of a send as the ack — same heuristic as the Python `phone_wire.WireClient`.
 */
class WireClient(private val ctx: Context) {

    private var service: BleRobotService? = null
    private var bound = false

    @Volatile
    private var pendingAck: CompletableFuture<String>? = null

    private val lineCb: (String) -> Unit = { line ->
        val p = pendingAck
        if (p != null && !p.isDone) {
            p.complete(line)
        }
    }

    private val conn = object : ServiceConnection {
        override fun onServiceConnected(name: ComponentName?, binder: IBinder?) {
            val b = binder as? BleRobotService.LocalBinder ?: return
            service = b.service()
            service?.addLineListener(lineCb)
            bound = true
            RobotState.appendLog("[wire] bound to BleRobotService")
        }
        override fun onServiceDisconnected(name: ComponentName?) {
            service?.removeLineListener(lineCb)
            service = null
            bound = false
        }
    }

    fun bind() {
        if (bound) return
        val i = Intent(ctx, BleRobotService::class.java)
        ctx.bindService(i, conn, Context.BIND_AUTO_CREATE)
    }

    fun unbind() {
        try {
            service?.removeLineListener(lineCb)
            if (bound) ctx.unbindService(conn)
        } catch (_: Throwable) {}
        bound = false
        service = null
    }

    fun bleStatus(): String = service?.currentBleState() ?: "unbound"

    /** Send a command dict and wait up to [timeoutMs] for any reply line.
     *  If nothing arrives we still return a synthetic ack so the planner
     *  keeps moving (the Python wire client has the same behaviour). */
    fun send(cmd: JSONObject, timeoutMs: Long = 500): JSONObject {
        val svc = service ?: return JSONObject().put("ok", false).put("err", "unbound")
        val fut = CompletableFuture<String>()
        pendingAck = fut
        val enqueued = svc.sendLine(cmd.toString())
        if (!enqueued) {
            pendingAck = null
            return JSONObject().put("ok", false).put("err", "ble_not_ready")
                .put("queued", true)
        }
        val line = try {
            fut.get(timeoutMs, TimeUnit.MILLISECONDS)
        } catch (_: Throwable) { null }
        pendingAck = null
        val ack = JSONObject().put("ok", true).put("sent", cmd)
        if (line != null) {
            try {
                ack.put("reply", JSONObject(line))
            } catch (_: Throwable) {
                ack.put("reply_raw", line)
            }
        }
        return ack
    }
}

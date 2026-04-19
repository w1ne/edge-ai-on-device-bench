package dev.robot.companion

import android.util.Log
import java.io.BufferedReader
import java.io.InputStreamReader
import java.io.OutputStream
import java.net.InetAddress
import java.net.ServerSocket
import java.net.Socket
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.Executors
import kotlin.concurrent.thread

/**
 * Tiny multi-client line server on 127.0.0.1:[port].
 * - accepts: each client runs on its own reader thread
 * - fan-out: broadcastLine writes "line\n" to every connected client
 * - onLineFromClient: newline-delimited lines from any client surface here
 */
class LocalTcpServer(
    private val port: Int,
    private val onClientCountChanged: (Int) -> Unit,
    private val onLineFromClient: (String) -> Unit,
) {
    companion object {
        private const val TAG = "LocalTcpServer"
    }

    private var server: ServerSocket? = null
    private val clients = CopyOnWriteArrayList<ClientConn>()
    @Volatile private var running = false

    /** All socket writes go through this single-thread executor so we never block main. */
    private val writer = Executors.newSingleThreadExecutor { r ->
        Thread(r, "tcp-writer").apply { isDaemon = true }
    }

    fun start() {
        if (running) return
        running = true
        thread(name = "tcp-accept", isDaemon = true) { acceptLoop() }
    }

    fun stop() {
        running = false
        try { server?.close() } catch (_: Throwable) {}
        for (c in clients) c.close()
        clients.clear()
        onClientCountChanged(0)
        writer.shutdownNow()
    }

    /** Fan-out a line to every connected client. Safe to call from any thread. */
    fun broadcastLine(line: String) {
        val payload = (line + "\n").toByteArray(Charsets.UTF_8)
        val snapshot = clients.toList()
        writer.execute {
            val dead = mutableListOf<ClientConn>()
            for (c in snapshot) {
                try {
                    synchronized(c.out) {
                        c.out.write(payload)
                        c.out.flush()
                    }
                } catch (e: Throwable) {
                    Log.w(TAG, "client write failed: ${e.javaClass.simpleName}: ${e.message}")
                    dead += c
                }
            }
            if (dead.isNotEmpty()) {
                for (d in dead) {
                    d.close()
                    clients.remove(d)
                }
                onClientCountChanged(clients.size)
            }
        }
    }

    private fun acceptLoop() {
        val bindErrBackoffMs = longArrayOf(500, 1000, 2000, 4000)
        var attempt = 0
        while (running) {
            try {
                val s = ServerSocket(port, 8, InetAddress.getByName("127.0.0.1"))
                server = s
                Log.i(TAG, "listening on 127.0.0.1:$port")
                attempt = 0
                while (running) {
                    val sock = try {
                        s.accept()
                    } catch (e: Throwable) {
                        if (running) Log.w(TAG, "accept failed: ${e.message}")
                        break
                    }
                    onClientAccepted(sock)
                }
            } catch (e: Throwable) {
                Log.e(TAG, "bind failed on :$port: ${e.message}")
                val idx = attempt.coerceAtMost(bindErrBackoffMs.size - 1)
                Thread.sleep(bindErrBackoffMs[idx])
                attempt++
            } finally {
                try { server?.close() } catch (_: Throwable) {}
                server = null
            }
        }
    }

    private fun onClientAccepted(sock: Socket) {
        sock.tcpNoDelay = true
        val c = ClientConn(sock, sock.getOutputStream())
        clients.add(c)
        onClientCountChanged(clients.size)
        thread(name = "tcp-client-${sock.port}", isDaemon = true) {
            try {
                val br = BufferedReader(InputStreamReader(sock.getInputStream(), Charsets.UTF_8))
                while (running) {
                    val line = br.readLine() ?: break
                    if (line.isNotBlank()) onLineFromClient(line)
                }
            } catch (e: Throwable) {
                Log.i(TAG, "client reader ended: ${e.message}")
            } finally {
                c.close()
                clients.remove(c)
                onClientCountChanged(clients.size)
            }
        }
    }

    private class ClientConn(val sock: Socket, val out: OutputStream) {
        fun close() {
            try { sock.close() } catch (_: Throwable) {}
        }
    }
}

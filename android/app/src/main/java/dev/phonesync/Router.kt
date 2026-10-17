package dev.phonesync

import android.util.Base64
import org.json.JSONObject
import java.util.concurrent.atomic.AtomicInteger

object Router {
    private val pairFailures = AtomicInteger()
    @Volatile private var pairLockUntil = 0L

    fun handle(req: Request, resp: Response) {
        var path = req.path
        var prefix = ""

        // Capability URL form: /t/<token>/... (used by Windows' WebDAV client, which won't send Basic auth over http)
        if (path.startsWith("/t/")) {
            val end = path.indexOf('/', 3).let { if (it < 0) path.length else it }
            val tok = path.substring(3, end)
            req.pc = Pcs.byToken(tok) ?: return resp.text(403, "bad token")
            prefix = path.substring(0, end)
            path = path.substring(end).ifEmpty { "/" }
        }

        when (path) {
            "/api/ping" -> return resp.json(Hub.pingJson())
            "/api/pair" -> return pair(req, resp)
        }

        if (req.pc == null) req.pc = Pcs.byToken(requestToken(req))
        val pc = req.pc ?: return resp.bytes(401, "unauthorized".toByteArray(), "text/plain",
            listOf("WWW-Authenticate" to "Basic realm=\"Symphone\""))
        Pcs.seen(pc, req.remote)
        Arrival.heardFrom(pc, req.remote)

        when {
            path == "/dav" || path.startsWith("/dav/") ->
                WebDav.handle(req, resp, path.removePrefix("/dav"), "$prefix/dav")
            path.startsWith("/api/") -> Api.handle(req, resp, path)
            path == "/" -> resp.json(Hub.pingJson())
            else -> resp.text(404, "not found")
        }
    }

    private fun requestToken(req: Request): String? {
        req.header("x-token")?.let { return it.trim() }
        req.query["t"]?.let { return it }
        val auth = req.header("authorization") ?: return null
        return when {
            auth.startsWith("Bearer ", true) -> auth.substring(7).trim()
            auth.startsWith("Basic ", true) -> try {
                String(Base64.decode(auth.substring(6).trim(), Base64.DEFAULT)).substringAfter(':')
            } catch (_: Exception) { null }
            else -> null
        }
    }

    /**
     * POST /api/pair {"code":"123456","pc":{"id","name"}} -> {"token","id","name"}. Code is shown in the phone app.
     * Without a "pc" object (v1 PCs) the legacy entry's token is returned.
     */
    private fun pair(req: Request, resp: Response) {
        val now = System.currentTimeMillis()
        if (now < pairLockUntil) return resp.text(429, "too many attempts")
        val body = try { JSONObject(req.bodyText()) } catch (_: Exception) { JSONObject() }
        val code = body.optString("code")
        if (code.length == 6 && Http.constEq(code, Hub.pairCode)) {
            pairFailures.set(0)
            Hub.rotatePairCode()
            val pcObj = body.optJSONObject("pc")
            val pc = pcObj?.optString("id")?.takeIf { it.isNotBlank() }
                ?.let { Pcs.getOrCreate(it, pcObj.optString("name")) } ?: Pcs.legacy()
            Pcs.seen(pc, req.remote)
            MainActivity.refresh()
            return resp.json(JSONObject().put("token", pc.token).put("id", Hub.deviceId).put("name", Hub.deviceName))
        }
        if (pairFailures.incrementAndGet() >= 5) {
            pairFailures.set(0)
            pairLockUntil = now + 60_000
            Hub.rotatePairCode()
            MainActivity.refresh()
        }
        resp.text(403, "wrong code")
    }
}

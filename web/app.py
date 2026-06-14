"""Mocha Web App — Full browser-based frontend for project mocha.

Serves static files and provides API endpoints for the web frontend.
The frontend connects directly to the bridge WebSocket for communication.

Usage:
    .venv/bin/python web/app.py
"""

import asyncio
import csv
from pathlib import Path

import httpx
import websockets as _wss
import yaml
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
import uvicorn

ROOT = Path(__file__).resolve().parent.parent
_cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
_bridge_port = _cfg.get("bridge", {}).get("port", 8090)
_web_port = _cfg.get("web", {}).get("port", 8080)

app = FastAPI(title="Mocha Web")


# ── Cache-control middleware ────────────────────────────────────────────────
# Cloudflare aggressively caches HTML/JS/CSS at the edge by default. That broke
# the welcome-first rollout: visitors got a stale app.js without tryAnonBootstrap
# and fell through to the old /login page. Force no-cache on the source-of-truth
# entry assets (HTML + JS + CSS) so deploys take effect immediately. Real binaries
# (images, VRM model, audio clips) keep a long max-age — those are versioned by
# filename and almost never change.
class CacheControlMiddleware(BaseHTTPMiddleware):
    """Tell Cloudflare (and the browser) what to cache vs always re-fetch."""

    NO_CACHE_SUFFIXES = (".html", ".js", ".css", ".json")
    LONG_CACHE_SUFFIXES = (
        ".vrm", ".jpg", ".jpeg", ".png", ".webp", ".gif",
        ".svg", ".ico", ".mp3", ".wav", ".ogg", ".mp4", ".woff", ".woff2",
    )

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        path = request.url.path

        # Never cache API/WS responses or HTML entry points (/, /login).
        if path.startswith(("/api/", "/admin/", "/ws/", "/chat/")) or path in ("/", "/login"):
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
            return response

        # Source files we deploy frequently — fetch every time, but allow
        # 304s via ETag/Last-Modified. Prevents Cloudflare from holding stale
        # JS/CSS at the edge after a code push.
        if path.endswith(self.NO_CACHE_SUFFIXES):
            response.headers["Cache-Control"] = "no-cache, must-revalidate"
            return response

        # Real binary assets — safe to cache for a day at the edge + browser.
        if path.endswith(self.LONG_CACHE_SUFFIXES):
            response.headers["Cache-Control"] = "public, max-age=86400"
            return response

        return response


app.add_middleware(CacheControlMiddleware)

# ── Static file routes ──────────────────────────────────────────────────────

STATIC_DIR = Path(__file__).resolve().parent / "static"


# index.html and login.html bootstrap every other resource — if the
# browser caches a stale copy, all the ?v=… cache-busters on JS/CSS
# inside it become stale too. Force fresh fetch on every visit so a
# deploy is always picked up by a single Ctrl+R.
_NO_CACHE_HEADERS = {
    "Cache-Control": "no-cache, no-store, must-revalidate",
    "Pragma": "no-cache",
    "Expires": "0",
}


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html", headers=_NO_CACHE_HEADERS)


@app.get("/gadget")
async def gadget():
    """Stable, named entry point for embedding Mocha (full VRM + voice) as the
    landing gadget on the opus-trading dashboard. Serves the same app as ``/``;
    kept as a separate route so the dashboard has a stable embed URL and a
    trimmed gadget view can be swapped in later without touching the dashboard.
    All assets/APIs use absolute paths, so this renders identically to ``/``.
    """
    return FileResponse(STATIC_DIR / "index.html", headers=_NO_CACHE_HEADERS)


@app.get("/login")
async def login_page():
    return FileResponse(STATIC_DIR / "login.html", headers=_NO_CACHE_HEADERS)


@app.get("/api/default-model")
async def default_model():
    """Serve the default VRM model for auto-loading."""
    vrm_path = ROOT / "character" / "Mocha.vrm"
    if not vrm_path.exists():
        return JSONResponse({"error": "No default VRM model found"}, status_code=404)
    return FileResponse(vrm_path, media_type="model/gltf-binary",
                        filename="Mocha.vrm")


@app.get("/api/config")
async def config():
    """Return configuration the frontend needs."""
    return {
        "bridge_port": _bridge_port,
        "bridge_ws_live": f"ws://{{host}}:{_bridge_port}/ws/live",
        "bridge_ws_monitor": f"ws://{{host}}:{_bridge_port}/ws/monitor",
        "bridge_http": f"http://{{host}}:{_bridge_port}",
    }


@app.get("/api/health")
async def health():
    """Probe all services and return combined health status.

    Memory and animation are in-process inside the bridge — the bridge's
    own /health endpoint already reports them, so we don't hit them
    separately here.
    """
    services = {
        "bridge": f"http://127.0.0.1:{_bridge_port}/health",
        "stt": f"http://127.0.0.1:{_cfg.get('stt', {}).get('port', 8091)}/health",
        "tts": f"http://127.0.0.1:{_cfg.get('tts', {}).get('port', 8092)}/health",
    }
    results = {}
    async with httpx.AsyncClient(timeout=2.0) as client:
        for name, url in services.items():
            try:
                resp = await client.get(url)
                results[name] = "ok" if resp.status_code == 200 else "error"
            except Exception:
                results[name] = "unreachable"
    all_ok = all(v == "ok" for v in results.values())
    return {"status": "healthy" if all_ok else "degraded", "services": results}


# ── Animation function table API ───────────────────────────────────────────

@app.get("/api/animation-functions")
async def animation_functions():
    """Return parsed animation_functions.csv as JSON for the animation controller."""
    csv_path = ROOT / "character" / "animation_functions.csv"
    if not csv_path.exists():
        return JSONResponse({"error": "animation_functions.csv not found"}, status_code=404)
    rows = []
    with open(csv_path, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append({
                "function": row.get("function", "").strip(),
                "description": row.get("description", "").strip(),
                "category": row.get("category", "").strip(),
                "looping": row.get("looping", "").strip().upper() == "TRUE",
                "animation": row.get("animation", "").strip() or None,
                "start_animation": row.get("start_animation", "").strip() or None,
                "loop_animation": row.get("loop_animation", "").strip() or None,
                "end_animation": row.get("end_animation", "").strip() or None,
                "max_repeats": int(row["max_repeats"].strip()) if row.get("max_repeats", "").strip() else None,
            })
    return {"functions": rows}


# ── /api/* proxy to the bridge ─────────────────────────────────────────────
# Anything under /api/ that isn't handled by one of the explicit routes above
# is forwarded transparently to the bridge. The frontend uses relative URLs
# (``fetch('/api/cron_jobs')``) because it doesn't know where the bridge is
# hosted; this proxy makes those calls just work regardless of whether the
# page is served from the web app (8080) or the bridge's /monitor (8000).

from fastapi import Request
from fastapi.responses import Response as _Response


_PROXY_BASE = f"http://127.0.0.1:{_bridge_port}"
_proxy_client: httpx.AsyncClient | None = None


async def _get_proxy_client() -> httpx.AsyncClient:
    global _proxy_client
    if _proxy_client is None:
        _proxy_client = httpx.AsyncClient(timeout=30.0, follow_redirects=False)
    return _proxy_client


async def _ws_relay(client: WebSocket, target_url: str) -> None:
    """Bidirectional WebSocket proxy: browser client <-> bridge."""
    try:
        async with _wss.connect(target_url) as bridge:
            async def c2b():
                try:
                    while True:
                        msg = await client.receive()
                        if msg["type"] == "websocket.disconnect":
                            break
                        if msg.get("bytes") is not None:
                            await bridge.send(msg["bytes"])
                        elif msg.get("text") is not None:
                            await bridge.send(msg["text"])
                except Exception:
                    pass
                finally:
                    await bridge.close()

            async def b2c():
                try:
                    async for message in bridge:
                        if isinstance(message, bytes):
                            await client.send_bytes(message)
                        else:
                            await client.send_text(message)
                except Exception:
                    pass

            tasks = [asyncio.create_task(c2b()), asyncio.create_task(b2c())]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass
    except Exception:
        pass
    finally:
        try:
            await client.close()
        except Exception:
            pass


@app.websocket("/ws/live")
async def _proxy_ws_live(client: WebSocket):
    qs = client.url.query
    target = f"ws://127.0.0.1:{_bridge_port}/ws/live" + (f"?{qs}" if qs else "")
    await client.accept()
    await _ws_relay(client, target)


@app.websocket("/ws/monitor")
async def _proxy_ws_monitor(client: WebSocket):
    qs = client.url.query
    target = f"ws://127.0.0.1:{_bridge_port}/ws/monitor" + (f"?{qs}" if qs else "")
    await client.accept()
    await _ws_relay(client, target)


@app.websocket("/ws/voice-stream")
async def _proxy_ws_voice(client: WebSocket):
    qs = client.url.query
    target = f"ws://127.0.0.1:{_bridge_port}/ws/voice-stream" + (f"?{qs}" if qs else "")
    await client.accept()
    await _ws_relay(client, target)


@app.get("/chat/stream", include_in_schema=False)
async def _proxy_chat_stream(request: Request):
    qs = request.url.query
    target = f"{_PROXY_BASE}/chat/stream" + (f"?{qs}" if qs else "")
    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in {"host", "content-length"}}

    async def _stream():
        async with httpx.AsyncClient(timeout=None) as hclient:
            async with hclient.stream("GET", target, headers=headers) as resp:
                async for chunk in resp.aiter_bytes():
                    yield chunk

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.api_route("/admin/{path:path}",
               methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
               include_in_schema=False)
async def _proxy_admin(path: str, request: Request):
    qs = request.url.query
    target = f"{_PROXY_BASE}/admin/{path}" + (f"?{qs}" if qs else "")
    body = await request.body()
    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in {"host", "content-length"}}
    try:
        client = await _get_proxy_client()
        resp = await client.request(
            request.method, target,
            content=body, headers=headers,
        )
    except Exception as exc:
        return JSONResponse({"error": f"bridge proxy error: {exc}"}, status_code=502)
    drop = {"content-encoding", "transfer-encoding", "connection"}
    out_headers = {k: v for k, v in resp.headers.items() if k.lower() not in drop}
    return _Response(content=resp.content, status_code=resp.status_code,
                     headers=out_headers, media_type=resp.headers.get("content-type"))


@app.get("/health", include_in_schema=False)
async def _proxy_health_fwd(request: Request):
    target = f"{_PROXY_BASE}/health"
    headers = {k: v for k, v in request.headers.items() if k.lower() != "host"}
    try:
        client = await _get_proxy_client()
        resp = await client.get(target, headers=headers)
    except Exception as exc:
        return JSONResponse({"error": f"bridge proxy error: {exc}"}, status_code=502)
    drop = {"content-encoding", "transfer-encoding", "connection"}
    out_headers = {k: v for k, v in resp.headers.items() if k.lower() not in drop}
    return _Response(content=resp.content, status_code=resp.status_code,
                     headers=out_headers, media_type=resp.headers.get("content-type"))


@app.api_route("/api/{path:path}",
               methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
               include_in_schema=False)
async def _proxy_api(path: str, request: Request):
    """Forward /api/* requests to the bridge."""
    qs = request.url.query
    target = f"{_PROXY_BASE}/api/{path}" + (f"?{qs}" if qs else "")
    body = await request.body()
    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in {"host", "content-length"}}
    try:
        client = await _get_proxy_client()
        resp = await client.request(
            request.method, target,
            content=body, headers=headers,
        )
    except Exception as exc:
        return JSONResponse({"error": f"bridge proxy error: {exc}"}, status_code=502)
    drop = {"content-encoding", "transfer-encoding", "connection"}
    out_headers = {k: v for k, v in resp.headers.items() if k.lower() not in drop}
    return _Response(content=resp.content, status_code=resp.status_code,
                     headers=out_headers, media_type=resp.headers.get("content-type"))


# Mount static files last so API routes take priority
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def _kill_port(port: int) -> None:
    """Kill any process listening on the given port."""
    import signal, subprocess
    try:
        out = subprocess.check_output(
            ["lsof", "-ti", f"tcp:{port}"], text=True
        ).strip()
        for pid in out.splitlines():
            os.kill(int(pid), signal.SIGKILL)
    except (subprocess.CalledProcessError, OSError):
        pass


if __name__ == "__main__":
    import os
    _kill_port(_web_port)
    print(f"Starting Mocha Web at http://localhost:{_web_port}")
    print(f"Bridge expected at ws://localhost:{_bridge_port}/ws/live")
    uvicorn.run(app, host="0.0.0.0", port=_web_port)

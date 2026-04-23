"""Mocha Web App — Full browser-based frontend for ProjectParrot.

Serves static files and provides API endpoints for the web frontend.
The frontend connects directly to the bridge WebSocket for communication.

Usage:
    .venv/bin/python web/app.py
"""

import csv
from pathlib import Path

import httpx
import yaml
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

ROOT = Path(__file__).resolve().parent.parent
_cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
_bridge_port = _cfg.get("bridge", {}).get("port", 8000)
_web_port = _cfg.get("web", {}).get("port", 8080)

app = FastAPI(title="Mocha Web")

# ── Static file routes ──────────────────────────────────────────────────────

STATIC_DIR = Path(__file__).resolve().parent / "static"


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


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
    """Probe all services and return combined health status."""
    services = {
        "bridge": f"http://127.0.0.1:{_bridge_port}/health",
        "stt": f"http://127.0.0.1:{_cfg.get('stt', {}).get('port', 8001)}/health",
        "tts": f"http://127.0.0.1:{_cfg.get('tts', {}).get('port', 8002)}/health",
        "memory": f"http://127.0.0.1:{_cfg.get('memory', {}).get('port', 8003)}/health",
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

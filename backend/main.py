"""
HTTP + WebSocket API for the ArduPilot telemetry web UI.
"""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from serial.tools import list_ports

from backend.mavlink_service import MavlinkService

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

service = MavlinkService()
_ws_clients: set[WebSocket] = set()


async def _telemetry_broadcaster() -> None:
    last_json: str | None = None
    while True:
        await asyncio.sleep(0.15)
        snap = service.snapshot()
        payload = json.dumps({"type": "telemetry", "data": snap})
        if payload == last_json:
            continue
        last_json = payload
        dead: list[WebSocket] = []
        for ws in _ws_clients:
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            _ws_clients.discard(ws)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(_telemetry_broadcaster())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    service.disconnect()


app = FastAPI(title="ArduPilot Telemetry UI API", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ConnectBody(BaseModel):
    port: str = Field(..., description="Serial port, e.g. COM3 on Windows")
    baud: int = Field(57600, ge=1200, le=921600)


class ModeBody(BaseModel):
    mode: str = Field(..., description="Flight mode name, e.g. GUIDED, LOITER, RTL")


class QuickActionBody(BaseModel):
    command: Literal[
        "rtl",
        "takeoff",
        "hover",
        "gps_hold",
        "manual",
        "circle_here",
        "visual_nav",
    ]
    altitude_m: float = Field(
        10.0,
        ge=1.0,
        le=500.0,
        description="Target altitude for takeoff (m AGL-style per MAV_CMD_NAV_TAKEOFF).",
    )
    radius_m: float = Field(
        60.0,
        ge=5.0,
        le=800.0,
        description="Orbit radius when circling with GNSS (MAV_CMD_DO_ORBIT).",
    )
    orbit_speed_ms: float = Field(
        4.0,
        ge=0.5,
        le=40.0,
        description="Tangential speed for DO_ORBIT (m/s).",
    )


@app.post("/api/quick-action")
def quick_action(body: QuickActionBody) -> dict[str, Any]:
    try:
        cmd = body.command
        if cmd == "rtl":
            mode = service.return_to_home()
            return {"ok": True, "command": cmd, "mode": mode}
        if cmd == "takeoff":
            service.takeoff(body.altitude_m)
            return {"ok": True, "command": cmd, "note": "Many frames must be armed and ready for NAV_TAKEOFF."}
        if cmd == "hover":
            mode = service.hover()
            return {"ok": True, "command": cmd, "mode": mode}
        if cmd == "gps_hold":
            mode = service.gps_assisted_mode()
            return {"ok": True, "command": cmd, "mode": mode}
        if cmd == "manual":
            mode = service.manual_style_mode()
            return {"ok": True, "command": cmd, "mode": mode}
        if cmd == "circle_here":
            detail = service.circle_here(body.radius_m, body.orbit_speed_ms)
            return {"ok": True, "command": cmd, **detail}
        if cmd == "visual_nav":
            mode = service.visual_navigation_mode()
            return {"ok": True, "command": cmd, "mode": mode}
        raise HTTPException(status_code=400, detail=f"Unsupported command: {cmd}")
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


class RebootBody(BaseModel):
    confirm: bool = False


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/serial-ports")
def serial_ports():
    out = []
    for p in list_ports.comports():
        out.append(
            {
                "device": p.device,
                "name": p.name,
                "description": p.description or "",
                "hwid": p.hwid or "",
            }
        )
    return {"ports": out}


@app.get("/api/telemetry")
def telemetry():
    return service.snapshot()


@app.post("/api/connect")
def connect(body: ConnectBody):
    try:
        if service.snapshot().get("connected"):
            service.disconnect()
        service.connect(body.port.strip(), baud=body.baud)
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        log.exception("connect")
        raise HTTPException(status_code=500, detail=str(e))
    return service.snapshot()


@app.post("/api/disconnect")
def disconnect():
    service.disconnect()
    return service.snapshot()


@app.post("/api/arm")
def arm():
    try:
        service.arm()
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True}


@app.post("/api/disarm")
def disarm():
    try:
        service.disarm()
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True}


@app.post("/api/mode")
def set_mode(body: ModeBody):
    try:
        service.set_mode(body.mode.strip())
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True}


@app.post("/api/reboot-fc")
def reboot_fc(body: RebootBody):
    try:
        service.reboot_fc(confirm=body.confirm)
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True}


@app.websocket("/ws/telemetry")
async def ws_telemetry(ws: WebSocket):
    await ws.accept()
    _ws_clients.add(ws)
    try:
        await ws.send_text(json.dumps({"type": "telemetry", "data": service.snapshot()}))
        while True:
            try:
                await asyncio.wait_for(ws.receive_text(), timeout=120.0)
            except asyncio.TimeoutError:
                pass
    except WebSocketDisconnect:
        pass
    finally:
        _ws_clients.discard(ws)


# Static UI (sibling folder `static/` from repo root)
_ROOT = Path(__file__).resolve().parent.parent
_STATIC = _ROOT / "static"
if _STATIC.is_dir():
    app.mount("/assets", StaticFiles(directory=str(_STATIC)), name="assets")


@app.get("/")
def index_page():
    index = _STATIC / "index.html"
    if not index.is_file():
        raise HTTPException(status_code=404, detail="static/index.html missing — add UI files.")
    return FileResponse(index)


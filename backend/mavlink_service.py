"""
MAVLink over serial (USB 915 MHz telemetry dongles expose a COM port).
Thread-safe connection + telemetry state for the FastAPI layer.
"""
from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from pymavlink import mavutil

log = logging.getLogger(__name__)

_MULTICOPTER_TYPES: frozenset[int] = frozenset(
    {
        mavutil.mavlink.MAV_TYPE_QUADROTOR,
        mavutil.mavlink.MAV_TYPE_COAXIAL,
        mavutil.mavlink.MAV_TYPE_HELICOPTER,
        mavutil.mavlink.MAV_TYPE_HEXAROTOR,
        mavutil.mavlink.MAV_TYPE_OCTOROTOR,
        mavutil.mavlink.MAV_TYPE_TRICOPTER,
        mavutil.mavlink.MAV_TYPE_DODECAROTOR,
        mavutil.mavlink.MAV_TYPE_DECAROTOR,
    }
)

# MAVLink common — not exposed in older pymavlink builds
_MAV_CMD_DO_ORBIT = 34
_ORBIT_YAW_UNCHANGED = 0.0


def _is_multicopter(mav_type: int | None) -> bool:
    if mav_type is None:
        return False
    return int(mav_type) in _MULTICOPTER_TYPES


@dataclass
class TelemetryState:
    connected: bool = False
    port: str | None = None
    baud: int | None = None
    last_error: str | None = None
    target_system: int = 1
    target_component: int = 1
    heartbeat: dict[str, Any] = field(default_factory=dict)
    gps: dict[str, Any] = field(default_factory=dict)
    attitude: dict[str, Any] = field(default_factory=dict)
    vfr_hud: dict[str, Any] = field(default_factory=dict)
    sys_status: dict[str, Any] = field(default_factory=dict)
    last_message_type: str | None = None
    messages_received: int = 0

    def as_public_dict(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "port": self.port,
            "baud": self.baud,
            "last_error": self.last_error,
            "target_system": self.target_system,
            "target_component": self.target_component,
            "heartbeat": dict(self.heartbeat),
            "gps": dict(self.gps),
            "attitude": dict(self.attitude),
            "vfr_hud": dict(self.vfr_hud),
            "sys_status": dict(self.sys_status),
            "last_message_type": self.last_message_type,
            "messages_received": self.messages_received,
        }


def _mode_string(base_mode: int, custom_mode: int) -> str:
    try:
        return mavutil.mode_string_v10(base_mode, custom_mode)
    except Exception:
        return f"custom:{custom_mode}"


class MavlinkService:
    """Owns pymavlink connection and a recv loop thread."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._master: mavutil.mavlink_connection | None = None
        self._recv_thread: threading.Thread | None = None
        self._running = False
        self.state = TelemetryState()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self.state.as_public_dict()

    def connect(self, port: str, baud: int = 57600, timeout: float = 5.0) -> None:
        with self._lock:
            if self._master is not None:
                raise RuntimeError("Already connected — disconnect first.")
            self.state.last_error = None
            try:
                master = mavutil.mavlink_connection(port, baud=baud)
            except Exception as e:
                self.state.last_error = str(e)
                self.state.connected = False
                log.exception("Serial open failed")
                raise
            try:
                master.wait_heartbeat(timeout=timeout)
            except Exception as e:
                try:
                    master.close()
                except Exception:
                    pass
                self._master = None
                self.state.last_error = f"No heartbeat ({e}) — check port, baud, link power, same MAVLink version."
                self.state.connected = False
                log.warning("Heartbeat wait failed: %s", e)
                raise RuntimeError(self.state.last_error) from e
            hb = master.messages.get("HEARTBEAT")
            if hb:
                self.state.target_system = hb.get_srcSystem()
                self.state.target_component = hb.get_srcComponent()
            self._master = master
            self._running = True
            self.state.connected = True
            self.state.port = port
            self.state.baud = baud
            self.state.messages_received = 0
            self._recv_thread = threading.Thread(target=self._recv_loop, name="mavlink-recv", daemon=True)
            self._recv_thread.start()
            log.info("Connected %s @ %s", port, baud)

    def disconnect(self) -> None:
        with self._lock:
            self._running = False
            master = self._master
            self._master = None
        if master is not None:
            try:
                master.close()
            except Exception as e:
                log.warning("Close: %s", e)
        t = self._recv_thread
        self._recv_thread = None
        if t is not None and t.is_alive():
            t.join(timeout=2.0)
        with self._lock:
            self.state.connected = False
            self.state.port = None
            self.state.baud = None
        log.info("Disconnected")

    def _recv_loop(self) -> None:
        while True:
            with self._lock:
                if not self._running:
                    break
                master = self._master
            if master is None:
                break
            try:
                msg = master.recv_match(blocking=True, timeout=0.5)
            except Exception as e:
                log.warning("recv_match: %s", e)
                time.sleep(0.2)
                continue
            if msg is None:
                continue
            self._apply_message(msg)

    def _apply_message(self, msg: Any) -> None:
        mtype = msg.get_type()
        with self._lock:
            self.state.last_message_type = mtype
            self.state.messages_received += 1
            if mtype == "HEARTBEAT":
                armed = bool(msg.base_mode & mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED)
                self.state.heartbeat = {
                    "armed": armed,
                    "mode": _mode_string(msg.base_mode, msg.custom_mode),
                    "type": int(msg.type),
                    "autopilot": int(msg.autopilot),
                    "base_mode": int(msg.base_mode),
                    "custom_mode": int(msg.custom_mode),
                    "system_status": int(msg.system_status),
                }
                self.state.target_system = msg.get_srcSystem()
                self.state.target_component = msg.get_srcComponent()
            elif mtype == "GLOBAL_POSITION_INT":
                self.state.gps = {
                    "lat": msg.lat / 1e7,
                    "lon": msg.lon / 1e7,
                    "alt_m": msg.alt / 1000.0,
                    "relative_alt_m": msg.relative_alt / 1000.0,
                    "hdg_deg": msg.hdg / 100.0 if msg.hdg != 65535 else None,
                }
            elif mtype == "ATTITUDE":
                self.state.attitude = {
                    "roll_deg": math.degrees(msg.roll),
                    "pitch_deg": math.degrees(msg.pitch),
                    "yaw_deg": math.degrees(msg.yaw),
                }
            elif mtype == "VFR_HUD":
                self.state.vfr_hud = {
                    "airspeed": float(msg.airspeed),
                    "groundspeed": float(msg.groundspeed),
                    "heading": int(msg.heading),
                    "throttle_pct": int(msg.throttle),
                    "alt_m": float(msg.alt),
                    "climb_m_s": float(msg.climb),
                }
            elif mtype == "SYS_STATUS":
                v = msg.voltage_battery
                self.state.sys_status = {
                    "voltage_v": v / 1000.0 if v != 65535 else None,
                    "battery_remaining_pct": int(msg.battery_remaining)
                    if msg.battery_remaining != -1
                    else None,
                }

    def _require_master(self) -> mavutil.mavlink_connection:
        with self._lock:
            if self._master is None:
                raise RuntimeError("Not connected.")
            return self._master

    def set_mode(self, mode_name: str) -> None:
        master = self._require_master()
        with self._lock:
            tsys = self.state.target_system
            tcomp = self.state.target_component
        mode_id = master.mode_mapping().get(mode_name.upper())
        if mode_id is None:
            valid = ", ".join(sorted(master.mode_mapping().keys()))
            raise ValueError(f"Unknown mode {mode_name!r}. Available: {valid}")
        master.mav.set_mode_send(tsys, mavutil.mavlink.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED, mode_id)

    def _mav_type(self) -> int | None:
        with self._lock:
            t = self.state.heartbeat.get("type")
            return int(t) if t is not None else None

    def set_mode_first_available(self, names: list[str]) -> str:
        """Try mode names in order; return the first that exists on this vehicle."""
        master = self._require_master()
        mapping = master.mode_mapping()
        tried: list[str] = []
        for raw in names:
            key = raw.strip().upper()
            tried.append(key)
            mode_id = mapping.get(key)
            if mode_id is None:
                continue
            with self._lock:
                tsys = self.state.target_system
            master.mav.set_mode_send(
                tsys, mavutil.mavlink.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED, mode_id
            )
            return key
        valid = ", ".join(sorted(mapping.keys()))
        raise ValueError(
            f"None of {tried} exist on this firmware. Available modes: {valid}"
        )

    def return_to_home(self) -> str:
        return self.set_mode_first_available(["RTL"])

    def takeoff(self, altitude_m: float = 10.0) -> None:
        """MAV_CMD_NAV_TAKEOFF — copter uses param7 as climb-to altitude (m)."""
        master = self._require_master()
        if altitude_m < 0.5 or altitude_m > 500:
            raise ValueError("altitude_m must be between 0.5 and 500.")
        with self._lock:
            tsys = self.state.target_system
            tcomp = self.state.target_component
        master.mav.command_long_send(
            tsys,
            tcomp,
            mavutil.mavlink.MAV_CMD_NAV_TAKEOFF,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            float(altitude_m),
        )

    def hover(self) -> str:
        """Station hold — LOITER on most ArduPilot frames."""
        return self.set_mode_first_available(["LOITER", "QLOITER"])

    def gps_assisted_mode(self) -> str:
        """GPS-aided position hold / nav-ready mode."""
        return self.set_mode_first_available(["LOITER", "AUTO", "GUIDED", "QLOITER"])

    def manual_style_mode(self) -> str:
        """Pilot stick primary — stabilize / manual depending on frame."""
        t = self._mav_type()
        if _is_multicopter(t):
            return self.set_mode_first_available(["STABILIZE", "ACRO", "ALT_HOLD"])
        if t == mavutil.mavlink.MAV_TYPE_FIXED_WING:
            return self.set_mode_first_available(["MANUAL", "FBWA"])
        if t == mavutil.mavlink.MAV_TYPE_GROUND_ROVER:
            return self.set_mode_first_available(["MANUAL", "HOLD"])
        return self.set_mode_first_available(["STABILIZE", "MANUAL", "ACRO"])

    def circle_here(self, radius_m: float = 60.0, velocity_ms: float = 4.0) -> dict[str, Any]:
        """
        Orbit current GNSS position (MAV_CMD_DO_ORBIT) when lat/lon known;
        otherwise fall back to CIRCLE / LOITER mode only.
        """
        if radius_m < 5 or radius_m > 800:
            raise ValueError("radius_m must be between 5 and 800.")
        master = self._require_master()
        with self._lock:
            gps = dict(self.state.gps)
            tsys = self.state.target_system
            tcomp = self.state.target_component
            rel_alt = gps.get("relative_alt_m")
        lat = gps.get("lat")
        lon = gps.get("lon")
        if lat is not None and lon is not None and rel_alt is not None:
            alt = float(rel_alt)
            master.mav.command_int_send(
                tsys,
                tcomp,
                mavutil.mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT,
                _MAV_CMD_DO_ORBIT,
                0,
                0,
                float(radius_m),
                float(velocity_ms),
                _ORBIT_YAW_UNCHANGED,
                0.0,
                int(lat * 1e7),
                int(lon * 1e7),
                alt,
            )
            return {"method": "DO_ORBIT", "radius_m": radius_m, "velocity_ms": velocity_ms}

        t = self._mav_type()
        if _is_multicopter(t):
            mode = self.set_mode_first_available(["CIRCLE", "LOITER"])
        else:
            mode = self.set_mode_first_available(["LOITER"])
        return {
            "method": "MODE_ONLY",
            "mode": mode,
            "note": "No recent GNSS position in link snapshot — set CIRCLE/LOITER only.",
        }

    def visual_navigation_mode(self) -> str:
        """
        Optical-flow style hold when supported (FLOWHOLD). Bricks+VIO still need
        companion vision mavlink feeds; this only selects FC modes.
        """
        try:
            return self.set_mode_first_available(["FLOWHOLD"])
        except ValueError:
            return self.set_mode_first_available(["GUIDED", "LOITER"])

    def arm(self) -> None:
        master = self._require_master()
        with self._lock:
            tsys = self.state.target_system
            tcomp = self.state.target_component
        master.mav.command_LONG_send(
            tsys,
            tcomp,
            mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM,
            0,
            1.0,
            0,
            0,
            0,
            0,
            0,
        )

    def disarm(self) -> None:
        master = self._require_master()
        with self._lock:
            tsys = self.state.target_system
            tcomp = self.state.target_component
        master.mav.command_LONG_send(
            tsys,
            tcomp,
            mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
        )

    def reboot_fc(self, confirm: bool = False) -> None:
        if not confirm:
            raise ValueError("Set confirm=true to reboot the flight controller.")
        master = self._require_master()
        with self._lock:
            tsys = self.state.target_system
            tcomp = mavutil.mavlink.MAV_COMP_ID_AUTOPILOT1
        master.mav.command_LONG_send(
            tsys,
            tcomp,
            mavutil.mavlink.MAV_CMD_PREFLIGHT_REBOOT_SHUTDOWN,
            0,
            1,
            0,
            0,
            0,
            0,
            0,
        )

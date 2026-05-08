# ArduPilot telemetry UI

Local web dashboard that talks **MAVLink** to an ArduPilot flight controller over a **USB serial** port. A **915 MHz** telemetry pair (SiK‑style radios, RFD900, etc.) normally shows up on the PC as ground‑side **COM/tty** — that is what this app opens; the RF band is handled by the modem firmware.

## Requirements

- Python **3.10+**
- Windows: USB drivers for your ground modem (often `STMicro` / `Silicon Labs` / `FTDI` depending on the board)
- ArduPilot vehicle with a compatible MAVLink air radio paired to the ground dongle
- Matching **baud** between the FC telem port, air radio, and ground USB serial (common values: **57600** or **115200**)

## Quick start

From this directory:

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
uvicorn backend.main:app --host 127.0.0.1 --port 8765
```

Open **http://127.0.0.1:8765** , choose the ground modem’s COM port, set baud, **Connect**.

On Windows you can double‑click **`run-server.bat`** after creating the venv once.

## Safety & compliance

- Operate only where local laws and your airspace rules allow.
- Test on the bench with **props removed** or the craft **restrained** before arming with loads installed.
- **Arm / mode change** from this UI affect the real vehicle. Confirm the COM port is the telemetry modem, not another device.

## Cursor / extending

The stack is small: **`backend/mavlink_service.py`** owns pymavlink serial I/O; **`backend/main.py`** exposes REST + WebSocket; **`static/`** is the UI. Add mission upload, geofence, or video in the backend and surface controls in the static app as needed.

## API sketch

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/serial-ports` | List COM/tty devices |
| POST | `/api/connect` | `{"port":"COM3","baud":57600}` |
| POST | `/api/disconnect` | Close serial |
| GET | `/api/telemetry` | Latest decoded snapshot |
| WebSocket | `/ws/telemetry` | JSON telemetry stream |
| POST | `/api/arm` / `/api/disarm` | MAV_CMD_COMPONENT_ARM_DISARM |
| POST | `/api/mode` | `{"mode":"LOITER"}` (names depend on frame type) |

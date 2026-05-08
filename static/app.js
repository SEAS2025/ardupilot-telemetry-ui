const $ = (id) => document.getElementById(id);

const api = (path, opts = {}) =>
  fetch(path, {
    headers: { "Content-Type": "application/json", ...opts.headers },
    ...opts,
  }).then(async (r) => {
    const text = await r.text();
    let data = null;
    try {
      data = text ? JSON.parse(text) : null;
    } catch {
      data = { detail: text || r.statusText };
    }
    if (!r.ok) {
      const msg = data?.detail ?? (typeof data === "string" ? data : JSON.stringify(data));
      throw new Error(msg);
    }
    return data;
  });

function fmtNum(n, digits = 1) {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return Number(n).toFixed(digits);
}

function setConnectedUI(connected) {
  const pill = $("connPill");
  const dot = $("connDot");
  const label = $("connLabel");
  if (connected) {
    pill.classList.add("online");
    label.textContent = "Linked";
  } else {
    pill.classList.remove("online");
    label.textContent = "Disconnected";
  }
}

let droneMap = null;
let droneMarker = null;
let mapUserPanned = false;
let mapDidInitialFit = false;
let lastGpsFix = null;

function gpsLooksValid(lat, lon) {
  if (lat == null || lon == null) return false;
  const a = Number(lat);
  const b = Number(lon);
  if (Number.isNaN(a) || Number.isNaN(b)) return false;
  if (Math.abs(a) > 90 || Math.abs(b) > 180) return false;
  if (Math.abs(a) < 1e-7 && Math.abs(b) < 1e-7) return false;
  return true;
}

function initDroneMap() {
  const el = $("droneMap");
  if (!el || typeof L === "undefined") return;
  droneMap = L.map("droneMap", { zoomControl: true }).setView([20, 0], 2);
  L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png", {
    attribution:
      '&copy; <a href="https://www.openstreetmap.org/copyright">OSM</a> &copy; <a href="https://carto.com/attributions">CARTO</a>',
    subdomains: "abcd",
    maxZoom: 20,
  }).addTo(droneMap);
  droneMarker = L.circleMarker([0, 0], {
    radius: 10,
    fillColor: "#3d9cf9",
    color: "#e8ecf4",
    weight: 2,
    opacity: 1,
    fillOpacity: 0.92,
  });
  droneMap.on("dragend", () => {
    mapUserPanned = true;
  });
  requestAnimationFrame(() => {
    if (droneMap) droneMap.invalidateSize();
  });
}

function resetMapTracking() {
  mapUserPanned = false;
  mapDidInitialFit = false;
  lastGpsFix = null;
  const statusEl = $("mapStatus");
  if (statusEl) statusEl.textContent = "Not connected";
  if (droneMap && droneMarker && droneMap.hasLayer(droneMarker)) {
    droneMap.removeLayer(droneMarker);
  }
}

function updateDroneMap(gps, heartbeat, vfr) {
  const statusEl = $("mapStatus");
  if (!droneMap || !droneMarker) return;

  const lat = gps?.lat;
  const lon = gps?.lon;

  if (!gpsLooksValid(lat, lon)) {
    lastGpsFix = null;
    if (statusEl) {
      statusEl.textContent =
        "Linked — waiting for GNSS position (GLOBAL_POSITION_INT)";
    }
    if (droneMap.hasLayer(droneMarker)) droneMap.removeLayer(droneMarker);
    return;
  }

  lastGpsFix = { lat, lon };
  const ll = [lat, lon];
  if (!droneMap.hasLayer(droneMarker)) {
    droneMarker.setLatLng(ll);
    droneMarker.addTo(droneMap);
  } else {
    droneMarker.setLatLng(ll);
  }

  const mode = heartbeat?.mode ?? "—";
  const armed =
    heartbeat?.armed === true
      ? "armed"
      : heartbeat?.armed === false
        ? "disarmed"
        : "?";
  const rel = gps?.relative_alt_m;
  const altLine =
    rel != null && !Number.isNaN(Number(rel))
      ? `<br>Rel alt ${Number(rel).toFixed(1)} m`
      : "";
  const gs = vfr?.groundspeed;
  const gsLine =
    gs != null && !Number.isNaN(Number(gs))
      ? `<br>GS ${Number(gs).toFixed(1)} m/s`
      : "";
  droneMarker.bindPopup(
    `<strong>Drone</strong><br>${fmtNum(lat, 6)}, ${fmtNum(lon, 6)}${altLine}<br>${mode} · ${armed}${gsLine}`
  );

  if (statusEl) {
    const bits = [
      `${fmtNum(lat, 5)}, ${fmtNum(lon, 5)}`,
      rel != null && !Number.isNaN(Number(rel)) ? ` · ${Number(rel).toFixed(0)} m AHL` : "",
    ];
    statusEl.textContent = bits.join("");
  }

  if (!mapUserPanned) {
    if (!mapDidInitialFit) {
      droneMap.setView(ll, 17, { animate: true });
      mapDidInitialFit = true;
    } else {
      droneMap.panTo(ll, { animate: false });
    }
  }
}

function applyTelemetry(data) {
  setConnectedUI(!!data.connected);
  $("errorBar").hidden = true;

  if (!data.connected) {
    resetMapTracking();
  }

  const hb = data.heartbeat || {};
  $("vMode").textContent = hb.mode ?? "—";
  $("vArmed").textContent =
    hb.armed === true ? "Yes" : hb.armed === false ? "No" : "—";
  $("vMsgs").textContent = String(data.messages_received ?? 0);
  $("vLast").textContent = data.last_message_type ?? "—";

  const v = data.vfr_hud || {};
  $("vGs").textContent =
    v.groundspeed != null ? `${fmtNum(v.groundspeed)} m/s` : "—";
  $("vAs").textContent = v.airspeed != null ? `${fmtNum(v.airspeed)} m/s` : "—";
  $("vAlt").textContent = v.alt_m != null ? `${fmtNum(v.alt_m)} m` : "—";
  $("vHdg").textContent =
    v.heading != null && v.heading !== 65535 ? `${v.heading}°` : "—";

  const a = data.attitude || {};
  $("vRoll").textContent =
    a.roll_deg != null ? `${fmtNum(a.roll_deg)}°` : "—";
  $("vPitch").textContent =
    a.pitch_deg != null ? `${fmtNum(a.pitch_deg)}°` : "—";
  $("vYaw").textContent = a.yaw_deg != null ? `${fmtNum(a.yaw_deg)}°` : "—";

  const g = data.gps || {};
  $("vLat").textContent = g.lat != null ? fmtNum(g.lat, 6) : "—";
  $("vLon").textContent = g.lon != null ? fmtNum(g.lon, 6) : "—";
  $("vRelAlt").textContent =
    g.relative_alt_m != null ? `${fmtNum(g.relative_alt_m)} m` : "—";

  const s = data.sys_status || {};
  $("vVolt").textContent =
    s.voltage_v != null ? `${fmtNum(s.voltage_v, 2)} V` : "—";
  $("vBat").textContent =
    s.battery_remaining_pct != null ? `${s.battery_remaining_pct}%` : "—";

  updateDroneMap(g, hb, v);

  if (data.last_error) {
    $("errorBar").textContent = data.last_error;
    $("errorBar").hidden = false;
  }
}

async function refreshPorts() {
  const { ports } = await api("/api/serial-ports");
  const sel = $("portSelect");
  sel.innerHTML = "";
  if (!ports.length) {
    const o = document.createElement("option");
    o.value = "";
    o.textContent = "No serial devices found";
    sel.appendChild(o);
    return;
  }
  for (const p of ports) {
    const o = document.createElement("option");
    o.value = p.device;
    o.textContent = `${p.device} — ${p.description || p.name}`;
    sel.appendChild(o);
  }
}

function showErr(e) {
  $("errorBar").textContent = e.message || String(e);
  $("errorBar").hidden = false;
}

function wsUrl() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${location.host}/ws/telemetry`;
}

function connectWebSocket() {
  const ws = new WebSocket(wsUrl());
  ws.onmessage = (ev) => {
    try {
      const msg = JSON.parse(ev.data);
      if (msg.type === "telemetry" && msg.data) applyTelemetry(msg.data);
    } catch (_) {
      /* ignore */
    }
  };
  ws.onclose = () => {
    setTimeout(connectWebSocket, 2000);
  };
  ws.onerror = () => {
    try {
      ws.close();
    } catch (_) {
      /* ignore */
    }
  };
  return ws;
}

async function postQuickAction(command) {
  if (command === "rtl") {
    if (!confirm("Engage return to home (RTL)? Vehicle will navigate to rally/home.")) return;
  } else if (command === "takeoff") {
    if (!confirm("Send takeoff command? Vehicle must be armed and ready per your checklist.")) return;
  } else if (command === "circle_here") {
    if (!confirm("Start orbit / circle at current position? Ensure altitude and airspace are safe.")) return;
  }
  const payload = {
    command,
    altitude_m: Math.min(500, Math.max(1, Number($("takeoffAlt").value) || 10)),
    radius_m: Math.min(800, Math.max(5, Number($("circleRadius").value) || 60)),
    orbit_speed_ms: Math.min(40, Math.max(0.5, Number($("orbitSpeed").value) || 4)),
  };
  return api("/api/quick-action", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

async function main() {
  initDroneMap();
  $("btnMapRecenter")?.addEventListener("click", () => {
    mapUserPanned = false;
    if (lastGpsFix && droneMap) {
      droneMap.setView([lastGpsFix.lat, lastGpsFix.lon], 17, { animate: true });
    }
  });

  document.querySelectorAll("[data-quick]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const command = btn.getAttribute("data-quick");
      try {
        await postQuickAction(command);
      } catch (e) {
        showErr(e);
      }
    });
  });
  $("btnRefreshPorts").addEventListener("click", () =>
    refreshPorts().catch(showErr)
  );
  $("btnConnect").addEventListener("click", async () => {
    const port = $("portSelect").value;
    const baud = Number($("baudSelect").value);
    if (!port) {
      showErr(new Error("Pick a serial port."));
      return;
    }
    try {
      const data = await api("/api/connect", {
        method: "POST",
        body: JSON.stringify({ port, baud }),
      });
      mapUserPanned = false;
      mapDidInitialFit = false;
      applyTelemetry(data);
    } catch (e) {
      showErr(e);
    }
  });
  $("btnDisconnect").addEventListener("click", async () => {
    try {
      const data = await api("/api/disconnect", { method: "POST" });
      applyTelemetry(data);
    } catch (e) {
      showErr(e);
    }
  });
  $("btnArm").addEventListener("click", async () => {
    if (!confirm("Arm the vehicle? Only if safe and legal.")) return;
    try {
      await api("/api/arm", { method: "POST" });
    } catch (e) {
      showErr(e);
    }
  });
  $("btnDisarm").addEventListener("click", async () => {
    try {
      await api("/api/disarm", { method: "POST" });
    } catch (e) {
      showErr(e);
    }
  });
  $("btnMode").addEventListener("click", async () => {
    const mode = $("modeInput").value.trim();
    if (!mode) {
      showErr(new Error("Enter a mode name."));
      return;
    }
    try {
      await api("/api/mode", {
        method: "POST",
        body: JSON.stringify({ mode }),
      });
    } catch (e) {
      showErr(e);
    }
  });

  await refreshPorts().catch(showErr);
  try {
    const snap = await api("/api/telemetry");
    applyTelemetry(snap);
  } catch (_) {
    /* backend may be down briefly */
  }
  connectWebSocket();
}

main();

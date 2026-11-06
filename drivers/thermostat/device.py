"""Honeywell T5 thermostat device — cloud polling + capability mapping + commands.

The T5 is cloud-only (Resideo API v2). We poll every few minutes, map the state
onto Homey capabilities, and send commands via read-modify-write of the
`changeableValues` object. All temperatures are presented to Homey in Celsius;
if the physical thermostat reports Fahrenheit we convert on the way in and send
setpoints back in the device's own unit.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime
from typing import Any

MAX_EVENTS = 15  # rows shown in the "Recent activity" settings group

# Make the app-root `honeywell` package importable (named `honeywell`, not
# `lib`, which the Homey runner already owns — see driver.py).
_APP_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _APP_ROOT not in sys.path:
    sys.path.insert(0, _APP_ROOT)

from homey.device import Device  # noqa: E402

from honeywell.api import (  # noqa: E402
    HoneywellAuthError,
    HoneywellClient,
    HoneywellError,
    HoneywellRateLimitError,
    OAuth2Auth,
    new_session,
)
from honeywell.const import (  # noqa: E402
    CLIENT_ID,
    CLIENT_SECRET,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_REDIRECT_URI,
    DEFAULT_SETPOINT_STATUS,
    FAN_API_TO_HOMEY,
    MODE_API_TO_HOMEY,
    MODE_HOMEY_TO_API,
    OPERATION_TO_STATE,
)


def _f_to_c(f: float) -> float:
    return (float(f) - 32.0) * 5.0 / 9.0


def _c_to_f(c: float) -> float:
    return float(c) * 9.0 / 5.0 + 32.0


class HoneywellThermostatDevice(Device):
    # ── Lifecycle ──────────────────────────────────────────────────────────
    async def on_init(self) -> None:
        store = self.get_store()
        self.device_id = store.get("device_id") or self.get_data().get("id")
        self.location_id = store.get("location_id")

        self._loop = asyncio.get_running_loop()
        self._shutting_down = False
        self._poll_task: asyncio.Task | None = None
        self._units = "Celsius"          # updated from each poll
        self._changeable: dict = {}      # last-seen changeableValues (for RMW)
        self._bounds_cache: dict | None = None  # last target_temperature options
        self._last_temp: float | None = None
        self._last_mode: str | None = None
        self._last_op_state: str | None = None
        self._last_available: bool | None = None
        self._last_heat: float | None = None
        self._last_cool: float | None = None
        self._active_side: str = "heat"  # which setpoint the dial follows
        # Rolling in-app event log (newest first), shown on the Settings page.
        # NOTE: named `_event_log`, NOT `_events` — the SDK's EventEmitter base
        # already owns `self._events` (its handler registry); shadowing it makes
        # every emit crash with "list indices must be integers or slices".
        self._event_log: list[str] = list(store.get("event_log") or [])

        self._session = new_session()
        # Credentials come from the app's baked-in shared developer app (const);
        # only the per-user OAuth tokens live in the device store.
        self._auth = OAuth2Auth(CLIENT_ID, CLIENT_SECRET, self._session, DEFAULT_REDIRECT_URI)
        self._auth.access_token = store.get("access_token")
        self._auth.refresh_token = store.get("refresh_token")
        self._client = HoneywellClient(self._auth, self._session)

        # Ensure the current capabilities exist on already-paired devices.
        for cap in (
            "measure_temperature", "measure_humidity", "target_temperature",
            "thermostat_mode", "honeywell_fan_mode", "honeywell_operating_state",
        ):
            if not self.has_capability(cap):
                try:
                    await self.add_capability(cap)
                except Exception as e:  # noqa: BLE001
                    self.error(f"add_capability {cap} failed:", e)

        self.register_capability_listener("target_temperature", self._on_set_target)
        self.register_capability_listener("thermostat_mode", self._on_set_mode)
        # Fan is read-only: Resideo's First Alert fan-write endpoint returns a
        # server-side 500 (JS-FA-Set-Fan-User-Input crashes), so we display the
        # fan mode but do not register a set listener.

        try:
            await self._auth.ensure_valid()
            await self._persist_tokens()
        except HoneywellError as e:
            self.error("Initial auth failed:", e)
            await self.set_unavailable("Sign-in failed — please re-pair the thermostat.")
            return

        await self._render_events()  # restore the log onto the Settings page
        await self._poll()
        self._poll_task = self._loop.create_task(self._poll_loop())
        self.log("Honeywell T5 device initialized:", self.device_id)

    async def on_uninit(self) -> None:
        self._shutting_down = True
        if self._poll_task:
            self._poll_task.cancel()
        if self._session and not self._session.closed:
            await self._session.close()

    async def on_deleted(self) -> None:
        await self.on_uninit()

    # ── Token persistence ───────────────────────────────────────────────────
    async def _persist_tokens(self) -> None:
        """Write rotated tokens back to the store so a restart stays signed in."""
        if not self._auth.tokens_rotated:
            return
        try:
            await self.set_store_value("refresh_token", self._auth.refresh_token)
            await self.set_store_value("access_token", self._auth.access_token)
            self._auth.tokens_rotated = False
        except Exception as e:  # noqa: BLE001
            self.error("persist tokens failed:", e)

    # ── Polling ──────────────────────────────────────────────────────────────
    async def _poll_loop(self) -> None:
        while not self._shutting_down:
            try:
                await asyncio.sleep(DEFAULT_POLL_INTERVAL)
                await self._poll()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.error("poll loop error:", e)

    async def _poll(self) -> None:
        try:
            await self._auth.ensure_valid()
            await self._persist_tokens()
            data = await self._client.get_thermostat(self.location_id, self.device_id)
        except HoneywellRateLimitError as e:
            self.log("Rate limited on poll; will retry next cycle:", e)
            return
        except HoneywellAuthError as e:
            self.error("Auth error on poll:", e)
            await self._set_avail(False, "Sign-in expired — please re-pair the thermostat.")
            return
        except HoneywellError as e:
            self.error("Poll error:", e)
            return
        if not data:
            return
        await self._set_avail(True)
        await self._apply_state(data)

    async def _apply_state(self, data: dict) -> None:
        self._units = data.get("units") or self._units
        cv = data.get("changeableValues") or {}
        self._changeable = dict(cv)

        # Indoor temperature / humidity. Store full precision; Homey floors the
        # °C→°F it shows, which matches the native app (a whole °F lands as X.999).
        temp = data.get("indoorTemperature")
        measure_c = None
        if isinstance(temp, (int, float)):
            measure_c = self._to_c(temp)
            await self._safe_set("measure_temperature", measure_c)
        hum = data.get("indoorHumidity")
        if isinstance(hum, (int, float)):
            await self._safe_set("measure_humidity", round(float(hum)))

        # Mode.
        api_mode = cv.get("mode")
        homey_mode = MODE_API_TO_HOMEY.get(api_mode)
        if homey_mode:
            await self._safe_set("thermostat_mode", homey_mode)

        # Operating state (also decides which setpoint is "in play" in Auto).
        op = (data.get("operationStatus") or {}).get("mode")
        op_state = OPERATION_TO_STATE.get(op, "idle")
        await self._safe_set("honeywell_operating_state", op_state)

        # Target temperature. Homey's native tile colors the whole screen and
        # writes "Heating/Cooling towards" purely from current-vs-target, with no
        # notion of an Auto heat/cool band. So we set the target to make that
        # inference CORRECT: the cool setpoint when the room is above it (target <
        # current → blue "cooling"), the heat setpoint when below it (target >
        # current → orange "heating"), and EQUAL to the current temperature when
        # in range (target == current → neutral, no false arrow).
        self._active_side, target_dev = self._display_target(
            op_state, temp, cv.get("heatSetpoint"), cv.get("coolSetpoint")
        )
        if isinstance(target_dev, (int, float)):
            await self._apply_target_bounds(data)
            if op_state in ("cooling", "heating") or measure_c is None:
                target_c = self._to_c(target_dev)
            else:
                # Idle: target == the exact current-temp value so the tile reads
                # "At temperature" with no false heating/cooling arrow.
                target_c = measure_c
            await self._safe_set("target_temperature", target_c)

        # Fan.
        fan = (((data.get("settings") or {}).get("fan") or {})
               .get("changeableValues") or {}).get("mode")
        homey_fan = FAN_API_TO_HOMEY.get(fan)
        if homey_fan:
            await self._safe_set("honeywell_fan_mode", homey_fan)

        # Operating-state event (externally driven — heating/cooling started).
        if self._last_op_state is not None and op_state != self._last_op_state:
            label = {
                "heating": "Heating started",
                "cooling": "Cooling started",
                "fan": "Fan running",
                "idle": "Idle",
            }.get(op_state, op_state)
            await self._log_event(label)
        self._last_op_state = op_state

        # Setpoint change (externally driven — e.g. schedule or the thermostat).
        h, c = cv.get("heatSetpoint"), cv.get("coolSetpoint")
        if (isinstance(h, (int, float)) and isinstance(c, (int, float))
                and self._last_heat is not None
                and (h != self._last_heat or c != self._last_cool)):
            await self._log_event(self._setpoint_text(api_mode, h, c))
        if isinstance(h, (int, float)):
            self._last_heat = h
        if isinstance(c, (int, float)):
            self._last_cool = c

        await self._fire_triggers(homey_mode)

    def _display_target(
        self, op_state: str, indoor: Any, heat: Any, cool: Any
    ) -> tuple[str, Any]:
        """Pick (active_side, target_value) so Homey's native tile matches the
        EQUIPMENT, not just the setpoint math. Homey infers the colour/message
        from current-vs-target, so we set the target to:
          • the cool setpoint while actually cooling  (target < current → blue)
          • the heat setpoint while actually heating  (target > current → orange)
          • the current temperature when idle          (target == current → neutral,
            "At temperature" — even if the room is a degree off a setpoint)
        `active_side` is which setpoint a dial edit should change.
        """
        h = heat if isinstance(heat, (int, float)) else None
        c = cool if isinstance(cool, (int, float)) else None
        i = indoor if isinstance(indoor, (int, float)) else None
        if op_state == "cooling" and c is not None:
            return "cool", c
        if op_state == "heating" and h is not None:
            return "heat", h
        # Idle / fan / off → neutral: target == current temperature.
        side = "heat"
        if c is not None and (h is None or i is None or abs(i - c) <= abs(i - h)):
            side = "cool"
        return side, (i if i is not None else (c if c is not None else h))

    async def _apply_target_bounds(self, data: dict) -> None:
        """Set target_temperature min/max from the API limits (in Celsius)."""
        if self._active_side == "cool":
            lo, hi = data.get("minCoolSetpoint"), data.get("maxCoolSetpoint")
        else:
            lo, hi = data.get("minHeatSetpoint"), data.get("maxHeatSetpoint")
        if not isinstance(lo, (int, float)) or not isinstance(hi, (int, float)):
            return
        opts = {
            "min": round(self._to_c(lo), 2),
            "max": round(self._to_c(hi), 2),
            # Fine step: a 0.5 °C step snaps the target's DISPLAY up to the next
            # half-degree, so a target that equals the current temp (e.g. both
            # 24.44 °C = 76 °F) renders one °F higher and Homey shows a false
            # "Heating towards". A fine step keeps target and measure in lockstep.
            "step": 0.1,
        }
        if self._bounds_cache == opts:
            return
        try:
            await self.set_capability_options("target_temperature", opts)
            self._bounds_cache = opts
        except Exception as e:  # noqa: BLE001
            self.error("set target bounds failed:", e)

    # ── Unit helpers ────────────────────────────────────────────────────────
    def _is_fahrenheit(self) -> bool:
        return str(self._units).lower().startswith("f")

    def _to_c(self, device_temp: float) -> float:
        """Device native unit -> Celsius (Homey stores Celsius). No bias/rounding:
        Homey quantises the DISPLAY per the capability's `decimals` (which we set
        to 1, so ~0.18 °F steps — enough to land near a whole °F; `decimals: 0`
        rounded to whole °C ≈ 2 °F steps and skipped 76 °F entirely)."""
        return _f_to_c(float(device_temp)) if self._is_fahrenheit() else float(device_temp)

    def _to_device(self, celsius: float) -> float:
        """Celsius (from Homey) -> device native unit (what the API expects)."""
        if self._is_fahrenheit():
            return round(_c_to_f(celsius))          # whole degrees F
        return round(float(celsius) * 2) / 2         # half-degree C

    # ── Commands (read-modify-write on changeableValues) ─────────────────────
    async def _post_changeable(self, edits: dict) -> None:
        await self._auth.ensure_valid()
        await self._persist_tokens()
        # Start from the freshest changeableValues we have; refetch if empty.
        base = dict(self._changeable)
        if not base:
            data = await self._client.get_thermostat(self.location_id, self.device_id)
            base = dict((data or {}).get("changeableValues") or {})
        base.update(edits)
        # T5 (LCC) needs an explicit hold for the change to actually take effect.
        base.setdefault("thermostatSetpointStatus", DEFAULT_SETPOINT_STATUS)
        base["thermostatSetpointStatus"] = DEFAULT_SETPOINT_STATUS
        await self._client.set_thermostat(self.location_id, self.device_id, base)
        self._changeable = base

    async def async_set_target(self, celsius: float) -> None:
        device_val = self._to_device(celsius)
        # Edit the SAME side the dial currently shows (see _display_target),
        # so dragging the target in Auto changes the setpoint that's in play.
        side = getattr(self, "_active_side", None) or (
            "cool" if self._changeable.get("mode") == "Cool" else "heat"
        )
        if side == "cool":
            await self._post_changeable({"coolSetpoint": device_val})
            self._last_cool = device_val
        else:
            await self._post_changeable({"heatSetpoint": device_val})
            self._last_heat = device_val
        await self._log_event(f"Set to {device_val:g}{self._unit_suffix()}")

    async def async_set_mode(self, homey_mode: str) -> None:
        api_mode = MODE_HOMEY_TO_API.get(homey_mode)
        if not api_mode:
            self.error("Unknown mode:", homey_mode)
            return
        await self._post_changeable({"mode": api_mode})
        await self._safe_set("thermostat_mode", homey_mode)  # optimistic
        if homey_mode != self._last_mode:
            await self._log_event(f"Mode → {homey_mode.capitalize()}")
            self._last_mode = homey_mode  # prevent the next poll re-logging it

    def _round_setpoint(self, value: float) -> float:
        """Round a value already in the DEVICE's unit to what the API accepts:
        whole degrees for Fahrenheit, half degrees for Celsius."""
        if self._is_fahrenheit():
            return float(round(float(value)))
        return round(float(value) * 2) / 2

    async def async_set_heat_cool(self, heat: float, cool: float) -> None:
        """Set both setpoints at once (for Auto mode). heat/cool are in the
        thermostat's own unit, sent straight through to the API."""
        h = self._round_setpoint(heat)
        c = self._round_setpoint(cool)
        if h >= c:
            raise ValueError(
                f"The heat setpoint ({h:g}) must be below the cool setpoint "
                f"({c:g}) — leave a few degrees between them."
            )
        await self._post_changeable({"heatSetpoint": h, "coolSetpoint": c})
        self._last_heat, self._last_cool = h, c  # prevent poll re-logging
        # Optimistically reflect the active-mode setpoint on the dial.
        mode = self._changeable.get("mode")
        active = c if mode == "Cool" else h
        await self._safe_set("target_temperature", round(self._to_c(active), 1))
        u = self._unit_suffix()
        await self._log_event(f"Setpoints: heat {h:g}{u} / cool {c:g}{u}")

    # ── Capability listeners ─────────────────────────────────────────────────
    async def _on_set_target(self, value: float, **kwargs) -> None:
        try:
            await self.async_set_target(float(value))
        except HoneywellError as e:
            self.error("set target failed:", e)
            raise Exception("Could not set the temperature on the thermostat.") from e

    async def _on_set_mode(self, value: str, **kwargs) -> None:
        try:
            await self.async_set_mode(value)
        except HoneywellError as e:
            self.error("set mode failed:", e)
            raise Exception("Could not change the thermostat mode.") from e

    # ── Helpers ──────────────────────────────────────────────────────────────
    async def _safe_set(self, cap: str, value: Any) -> None:
        if not self.has_capability(cap):
            return
        try:
            if self.get_capability_value(cap) != value:
                await self.set_capability_value(cap, value)
        except Exception as e:  # noqa: BLE001
            self.error(f"set {cap} failed:", e)

    # ── In-app event log (Settings → Recent activity) ────────────────────────
    def _unit_suffix(self) -> str:
        return "°F" if self._is_fahrenheit() else "°C"

    def _setpoint_text(self, api_mode: str | None, heat: float, cool: float) -> str:
        u = self._unit_suffix()
        if api_mode == "Auto":
            return f"Setpoints: heat {heat:g}{u} / cool {cool:g}{u}"
        active = cool if api_mode == "Cool" else heat
        return f"Set to {active:g}{u}"

    async def _log_event(self, msg: str) -> None:
        """Prepend a timestamped event, trim, persist, and refresh the settings."""
        stamp = datetime.now().strftime("%m/%d %H:%M")
        self._event_log.insert(0, f"{stamp}  {msg}")
        del self._event_log[MAX_EVENTS:]
        try:
            await self.set_store_value("event_log", self._event_log)
        except Exception as e:  # noqa: BLE001
            self.error("persist event_log failed:", e)
        await self._render_events()

    async def _render_events(self) -> None:
        """Write the current log into the read-only Settings labels."""
        desired = {
            f"event_{i + 1}": (self._event_log[i] if i < len(self._event_log) else "—")
            for i in range(MAX_EVENTS)
        }
        try:
            await self.set_settings(desired)
        except Exception as e:  # noqa: BLE001
            self.error("render events failed:", e)

    async def _set_avail(self, available: bool, reason: str = "") -> None:
        """Set availability and log online/offline transitions."""
        if available:
            await self.set_available()
        else:
            await self.set_unavailable(reason)
        if self._last_available is not None and available != self._last_available:
            await self._log_event("Back online" if available else "Went offline")
        self._last_available = available

    async def _fire_triggers(self, homey_mode: str | None) -> None:
        flow = self.homey.flow
        temp = self.get_capability_value("measure_temperature")
        if isinstance(temp, (int, float)) and temp != self._last_temp:
            if self._last_temp is not None:
                try:
                    await flow.get_device_trigger_card("temperature_changed").trigger(
                        self, {"temperature": float(temp)}
                    )
                except Exception:  # noqa: BLE001
                    pass
            self._last_temp = temp
        if homey_mode and homey_mode != self._last_mode:
            if self._last_mode is not None:
                try:
                    await flow.get_device_trigger_card("mode_changed").trigger(
                        self, {"mode": homey_mode}
                    )
                except Exception:  # noqa: BLE001
                    pass
                await self._log_event(f"Mode → {homey_mode.capitalize()}")
            self._last_mode = homey_mode


homey_export = HoneywellThermostatDevice

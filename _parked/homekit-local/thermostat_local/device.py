"""Honeywell T5 local (HomeKit) device — LAN control with push updates.

Reads and writes HAP characteristics directly over the network. HomeKit pushes
state changes to us (subscribe + dispatcher_connect), so there is no polling
loop and no rate limit — a slow safety-net refresh covers missed events.
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

_APP_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _APP_ROOT not in sys.path:
    sys.path.insert(0, _APP_ROOT)

from homey.device import Device  # noqa: E402

from honeywell.homekit import (  # noqa: E402
    HAP_MODE_TO_HOMEY,
    HAP_STATE_TO_HOMEY,
    HOMEY_MODE_TO_HAP,
    acquire_controller,
    map_fan,
    map_thermostat,
    release_controller,
)

_REFRESH_INTERVAL = 600  # seconds — safety net; real updates arrive by push


class HoneywellLocalDevice(Device):
    # ── Lifecycle ──────────────────────────────────────────────────────────
    async def on_init(self) -> None:
        store = self.get_store()
        self._hk_id = store.get("hk_device_id") or self.get_data().get("id")
        self._pairing_data = store.get("pairing_data") or {}

        self._loop = asyncio.get_running_loop()
        self._shutting_down = False
        self._pairing = None
        self._chars: dict[str, tuple[int, int]] = {}
        self._fan_char: tuple[int, int] | None = None
        self._unsub = None
        self._refresh_task: asyncio.Task | None = None
        self._last_temp: float | None = None
        self._last_mode: str | None = None

        if not self._pairing_data:
            await self.set_unavailable("No HomeKit pairing stored — please re-add the device.")
            return

        controller = await acquire_controller()
        try:
            self._pairing = controller.load_pairing(self._hk_id, self._pairing_data)
            if self._pairing is None:
                raise RuntimeError("Could not restore the HomeKit pairing")
            await self._pairing.async_populate_accessories_state()
        except Exception as e:  # noqa: BLE001
            self.error("HomeKit connect failed:", e)
            await release_controller()
            await self.set_unavailable(
                "Could not reach the thermostat on the network. "
                "Check it is powered on and on the same Wi-Fi as Homey."
            )
            return

        accessories = self._pairing.accessories
        self._chars = map_thermostat(accessories)
        if not self._chars:
            self.error("Accessory exposes no Thermostat service")
            await self.set_unavailable("This accessory does not expose a thermostat service.")
            return
        self._fan_char = map_fan(accessories)

        # The T5 often exposes no fan over HomeKit — only offer the capability
        # when the accessory actually has one.
        if self._fan_char and not self.has_capability("honeywell_fan_mode"):
            try:
                await self.add_capability("honeywell_fan_mode")
            except Exception as e:  # noqa: BLE001
                self.error("add fan capability failed:", e)
        elif not self._fan_char and self.has_capability("honeywell_fan_mode"):
            try:
                await self.remove_capability("honeywell_fan_mode")
            except Exception as e:  # noqa: BLE001
                self.error("remove fan capability failed:", e)

        self.register_capability_listener("target_temperature", self._on_set_target)
        self.register_capability_listener("thermostat_mode", self._on_set_mode)
        if self.has_capability("honeywell_fan_mode"):
            self.register_capability_listener("honeywell_fan_mode", self._on_set_fan)

        await self._apply_target_bounds()

        # Push updates: subscribe, then route events into capabilities.
        try:
            self._unsub = self._pairing.dispatcher_connect(self._on_event)
            await self._pairing.subscribe(list(self._chars.values()))
        except Exception as e:  # noqa: BLE001
            self.error("HomeKit subscribe failed (falling back to refresh):", e)

        await self.set_available()
        await self._refresh()
        self._refresh_task = self._loop.create_task(self._refresh_loop())
        self.log("Honeywell T5 local device initialized:", self._hk_id)

    async def on_uninit(self) -> None:
        self._shutting_down = True
        if self._refresh_task:
            self._refresh_task.cancel()
        if self._unsub:
            try:
                self._unsub()
            except Exception:  # noqa: BLE001
                pass
        if self._pairing:
            try:
                self._pairing.close()
            except Exception:  # noqa: BLE001
                pass
            self._pairing = None
            await release_controller()

    async def on_deleted(self) -> None:
        await self.on_uninit()

    # ── Bounds ──────────────────────────────────────────────────────────────
    async def _apply_target_bounds(self) -> None:
        """Take min/max/step for target_temperature from the accessory itself."""
        loc = self._chars.get("target_temperature")
        if not loc or not self._pairing:
            return
        aid, iid = loc
        char = None
        for accessory in self._pairing.accessories:
            if accessory.aid != aid:
                continue
            char = accessory.services.get_char_by_iid(iid)
            break
        if char is None:
            return
        opts: dict[str, Any] = {}
        if getattr(char, "minValue", None) is not None:
            opts["min"] = float(char.minValue)
        if getattr(char, "maxValue", None) is not None:
            opts["max"] = float(char.maxValue)
        if getattr(char, "minStep", None):
            opts["step"] = float(char.minStep)
        if not opts:
            return
        try:
            await self.set_capability_options("target_temperature", opts)
        except Exception as e:  # noqa: BLE001
            self.error("set target bounds failed:", e)

    # ── Inbound: push events + periodic safety refresh ──────────────────────
    def _on_event(self, event: dict) -> None:
        """HomeKit pushed new characteristic values (runs on the event loop)."""
        self._loop.create_task(self._handle_values(event))

    async def _refresh_loop(self) -> None:
        while not self._shutting_down:
            try:
                await asyncio.sleep(_REFRESH_INTERVAL)
                await self._refresh()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.error("refresh loop error:", e)

    async def _refresh(self) -> None:
        if not self._pairing:
            return
        try:
            values = await self._pairing.get_characteristics(list(self._chars.values()))
        except Exception as e:  # noqa: BLE001
            self.error("HomeKit read failed:", e)
            await self.set_unavailable("Lost contact with the thermostat.")
            return
        await self.set_available()
        await self._handle_values(values)

    async def _handle_values(self, values: dict) -> None:
        """Map {(aid, iid): {'value': X}} onto Homey capabilities."""
        by_loc = {loc: name for name, loc in self._chars.items()}
        mode_slug = None
        for loc, payload in (values or {}).items():
            name = by_loc.get(loc)
            if name is None or not isinstance(payload, dict) or "value" not in payload:
                continue
            value = payload["value"]
            try:
                if name == "current_temperature":
                    await self._safe_set("measure_temperature", round(float(value), 1))
                elif name == "current_humidity":
                    await self._safe_set("measure_humidity", round(float(value)))
                elif name == "target_temperature":
                    await self._safe_set("target_temperature", round(float(value), 1))
                elif name == "target_mode":
                    mode_slug = HAP_MODE_TO_HOMEY.get(int(value))
                    if mode_slug:
                        await self._safe_set("thermostat_mode", mode_slug)
                elif name == "current_mode":
                    await self._safe_set(
                        "honeywell_operating_state",
                        HAP_STATE_TO_HOMEY.get(int(value), "idle"),
                    )
            except (TypeError, ValueError) as e:
                self.error(f"bad value for {name}: {value!r}", e)
        await self._fire_triggers(mode_slug)

    # ── Commands ────────────────────────────────────────────────────────────
    async def _put(self, name: str, value: Any) -> None:
        loc = self._chars.get(name)
        if not loc or not self._pairing:
            raise Exception("That control is not available on this thermostat.")
        aid, iid = loc
        await self._pairing.put_characteristics([(aid, iid, value)])

    async def async_set_target(self, celsius: float) -> None:
        await self._put("target_temperature", float(celsius))
        await self._safe_set("target_temperature", round(float(celsius), 1))

    async def async_set_mode(self, homey_mode: str) -> None:
        hap = HOMEY_MODE_TO_HAP.get(homey_mode)
        if hap is None:
            self.error("Unknown mode:", homey_mode)
            return
        await self._put("target_mode", hap)
        await self._safe_set("thermostat_mode", homey_mode)

    async def async_set_fan(self, homey_fan: str) -> None:
        if not self._fan_char or not self._pairing:
            raise Exception("This thermostat exposes no fan control over HomeKit.")
        aid, iid = self._fan_char
        # Fan v2 TargetFanState: 0 = Manual/On, 1 = Auto. Active: 0/1.
        value = 1 if homey_fan == "auto" else 0
        await self._pairing.put_characteristics([(aid, iid, value)])
        await self._safe_set("honeywell_fan_mode", homey_fan)

    # ── Capability listeners ─────────────────────────────────────────────────
    async def _on_set_target(self, value: float, **kwargs) -> None:
        try:
            await self.async_set_target(float(value))
        except Exception as e:  # noqa: BLE001
            self.error("set target failed:", e)
            raise Exception("Could not set the temperature on the thermostat.") from e

    async def _on_set_mode(self, value: str, **kwargs) -> None:
        try:
            await self.async_set_mode(value)
        except Exception as e:  # noqa: BLE001
            self.error("set mode failed:", e)
            raise Exception("Could not change the thermostat mode.") from e

    async def _on_set_fan(self, value: str, **kwargs) -> None:
        try:
            await self.async_set_fan(value)
        except Exception as e:  # noqa: BLE001
            self.error("set fan failed:", e)
            raise Exception("Could not change the fan mode.") from e

    # ── Helpers ──────────────────────────────────────────────────────────────
    async def _safe_set(self, cap: str, value: Any) -> None:
        if not self.has_capability(cap):
            return
        try:
            if self.get_capability_value(cap) != value:
                await self.set_capability_value(cap, value)
        except Exception as e:  # noqa: BLE001
            self.error(f"set {cap} failed:", e)

    async def _fire_triggers(self, mode_slug: str | None) -> None:
        flow = self.homey.flow
        temp = self.get_capability_value("measure_temperature")
        if isinstance(temp, (int, float)) and temp != self._last_temp:
            if self._last_temp is not None:
                try:
                    await flow.get_device_trigger_card("temperature_changed_local").trigger(
                        self, {"temperature": float(temp)}
                    )
                except Exception:  # noqa: BLE001
                    pass
            self._last_temp = temp
        if mode_slug and mode_slug != self._last_mode:
            if self._last_mode is not None:
                try:
                    await flow.get_device_trigger_card("mode_changed_local").trigger(
                        self, {"mode": mode_slug}
                    )
                except Exception:  # noqa: BLE001
                    pass
            self._last_mode = mode_slug


homey_export = HoneywellLocalDevice

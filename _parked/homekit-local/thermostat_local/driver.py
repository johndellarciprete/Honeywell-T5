"""Honeywell T5 local (HomeKit) driver — LAN discovery and PIN pairing."""

from __future__ import annotations

import os
import sys
from typing import Any

# Make the app-root `honeywell` package importable (named `honeywell`, not
# `lib`, which the Homey runner already owns).
_APP_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _APP_ROOT not in sys.path:
    sys.path.insert(0, _APP_ROOT)

from homey.driver import Driver  # noqa: E402

from honeywell.homekit import (  # noqa: E402
    AlreadyPairedError,
    discover_thermostats,
    pair_accessory,
)


class HoneywellLocalDriver(Driver):
    async def on_init(self) -> None:
        self._register_flow()
        self.log("Honeywell T5 local (HomeKit) driver initialized")

    # ── Flow cards ─────────────────────────────────────────────────────────
    def _register_flow(self) -> None:
        flow = self.homey.flow

        async def set_mode_run(args, **kwargs):
            await args["device"].async_set_mode(args["mode"])

        async def set_fan_run(args, **kwargs):
            await args["device"].async_set_fan(args["mode"])

        flow.get_action_card("set_mode_local").register_run_listener(set_mode_run)
        flow.get_action_card("set_fan_mode_local").register_run_listener(set_fan_run)

    # ── Pairing ────────────────────────────────────────────────────────────
    async def on_pair(self, session) -> None:
        # Pair state lives on the DRIVER so it survives a PairSession restart
        # (see the Lymow app: a closure-local dict silently resets to empty).
        self._pair_seq = getattr(self, "_pair_seq", 0) + 1
        sid = self._pair_seq
        state: dict[str, Any] = getattr(self, "_pair_state_local", None) or {
            "found": [],
        }
        self._pair_state_local = state

        async def handle_discover(_data: Any = None) -> dict:
            """Scan the LAN for HomeKit accessories."""
            try:
                found = await discover_thermostats(timeout=12)
            except Exception as e:  # noqa: BLE001
                self.error("HomeKit discovery failed:", e)
                raise Exception(f"Discovery failed: {e}") from e
            state["found"] = found
            unpaired = [f for f in found if not f["paired"]]
            self.log(f"[pair {sid}] discovered {len(found)} accessory(ies), "
                     f"{len(unpaired)} unpaired")
            return {"accessories": found}

        async def handle_pair_device(data: dict) -> dict:
            """Pair with the chosen accessory using its on-screen setup code."""
            device_id = ((data or {}).get("device_id") or "").strip()
            pin = ((data or {}).get("pin") or "").strip()
            name = ((data or {}).get("name") or "Honeywell T5").strip()
            if not device_id:
                raise Exception("Pick an accessory first.")
            if not pin:
                raise Exception("Enter the setup code shown on the thermostat.")
            # aiohomekit wants the XXX-XX-XXX form; accept 8 bare digits too.
            digits = "".join(ch for ch in pin if ch.isdigit())
            if len(digits) == 8:
                pin = f"{digits[0:3]}-{digits[3:5]}-{digits[5:8]}"
            try:
                pairing_data = await pair_accessory(device_id, pin, alias=device_id)
            except AlreadyPairedError as e:
                raise Exception(str(e)) from e
            except Exception as e:  # noqa: BLE001
                self.error("HomeKit pairing failed:", e)
                raise Exception(
                    "Pairing failed. Check the code on the thermostat's screen "
                    "(the printed code often does not work) and try again."
                ) from e
            self.log(f"[pair {sid}] paired with {device_id}")
            return {
                "name": name,
                "data": {"id": device_id},
                "store": {"pairing_data": pairing_data, "hk_device_id": device_id},
            }

        session.set_handler("discover", handle_discover)
        session.set_handler("pair_device", handle_pair_device)


homey_export = HoneywellLocalDriver

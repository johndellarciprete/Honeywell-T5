"""Honeywell T5 driver — pairing (one-tap OAuth via Homey's cloud callback).

The developer-app credentials are baked into `honeywell.const` (one shared app
for every user), so pairing is just: pick which app you use, then sign in.
Pairing uses `homey.cloud.create_oauth2_callback`, so Homey catches the OAuth
redirect itself and hands the code back — nothing to copy or paste.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import sys
from typing import Any

# Make the app-root `honeywell` package importable from this nested driver
# module. NOTE: it is named `honeywell`, not `lib`, because the Homey runner
# already has its own top-level `lib` package in sys.modules.
_APP_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _APP_ROOT not in sys.path:
    sys.path.insert(0, _APP_ROOT)

from homey.driver import Driver, ListDeviceProperties  # noqa: E402

from honeywell.api import (  # noqa: E402
    HoneywellClient,
    HoneywellError,
    OAuth2Auth,
    device_id_of,
    device_label,
    iter_thermostats,
    new_session,
)
from honeywell.const import (  # noqa: E402
    CLIENT_ID,
    CLIENT_SECRET,
    DEFAULT_REDIRECT_URI,
)


class HoneywellThermostatDriver(Driver):
    async def on_init(self) -> None:
        self._register_flow()
        self.log("Honeywell T5 driver initialized")

    # ── Flow cards ─────────────────────────────────────────────────────────
    def _register_flow(self) -> None:
        flow = self.homey.flow

        async def set_mode_run(args, **kwargs):
            await args["device"].async_set_mode(args["mode"])

        async def set_heat_cool_run(args, **kwargs):
            await args["device"].async_set_heat_cool(args["heat"], args["cool"])

        flow.get_action_card("set_mode").register_run_listener(set_mode_run)
        flow.get_action_card("set_heat_cool").register_run_listener(set_heat_cool_run)

    # ── Pairing (standard login_oauth2 template) ────────────────────────────
    async def on_pair(self, session) -> None:
        # Pair state on the DRIVER so it survives a PairSession restart (a
        # closure-local dict would silently reset to an empty device list).
        self._pair_seq = getattr(self, "_pair_seq", 0) + 1
        sid = self._pair_seq
        state: dict[str, Any] = getattr(self, "_pair_state", None) or {
            "tokens": {},            # OAuth2Auth.to_dict()
            "thermostats": [],       # list of {location_id, device}
            "oauth_callback": None,  # keep a ref so it isn't GC'd mid-flow
        }
        self._pair_state = state
        self.log(f"[pair {sid}] session started")

        async def _finish(code: str) -> None:
            """Homey delivered the OAuth code: swap it for tokens + thermostats,
            then tell the login_oauth2 template to advance."""
            try:
                async with new_session() as http:
                    auth = OAuth2Auth(CLIENT_ID, CLIENT_SECRET, http, DEFAULT_REDIRECT_URI)
                    await auth.exchange_code(code)
                    client = HoneywellClient(auth, http)
                    thermostats = iter_thermostats(await client.get_locations())
                    state["tokens"] = auth.to_dict()
                state["thermostats"] = [
                    {"location_id": loc_id, "device": dev} for loc_id, dev in thermostats
                ]
                self.log(f"[pair {sid}] authorized -> {len(thermostats)} thermostat(s)")
            except Exception as e:  # noqa: BLE001
                self.error("OAuth finish error:", e)
            # Advance the template either way; list_devices reflects what we got.
            await session.emit("authorized")

        # Build the authorize URL up front and hand it to Homey's cloud callback.
        # `appSelect=1` lets the user pick First Alert vs Resideo on Honeywell's
        # own page — no account picker needed in Homey, and it works for both.
        oauth_state = secrets.token_urlsafe(24)
        async with new_session() as http:
            auth = OAuth2Auth(CLIENT_ID, CLIENT_SECRET, http, DEFAULT_REDIRECT_URI)
            url = auth.get_authorize_url(state=oauth_state, account_type="select")

        callback = await self.homey.cloud.create_oauth2_callback(url)
        state["oauth_callback"] = callback  # keep alive for the whole flow

        callback.on_url(lambda login_url: asyncio.create_task(session.emit("url", login_url)))
        callback.on_code(lambda code: asyncio.create_task(_finish(code)))
        self.log(f"[pair {sid}] oauth callback created")

        async def handle_list_devices(_data: Any = None) -> list[ListDeviceProperties]:
            out: list[ListDeviceProperties] = []
            tokens = state.get("tokens") or {}
            for entry in state["thermostats"]:
                dev = entry["device"]
                dev_id = device_id_of(dev)
                out.append({
                    "name": device_label(dev),
                    "data": {"id": dev_id},
                    "store": {
                        "device_id": dev_id,
                        "location_id": entry["location_id"],
                        "access_token": tokens.get("access_token"),
                        "refresh_token": tokens.get("refresh_token"),
                    },
                })
            self.log(f"[pair {sid}] list_devices -> {[o['name'] for o in out]}")
            return out

        session.set_handler("list_devices", handle_list_devices)


homey_export = HoneywellThermostatDriver

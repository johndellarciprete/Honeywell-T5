"""
Local HomeKit (HAP) transport for the Honeywell T5.

The T5 is a "Works with Apple HomeKit" accessory, which means it speaks the
HomeKit Accessory Protocol directly over the LAN — no Resideo cloud, no
developer API key, no OAuth. This module wraps `aiohomekit` (the same library
Home Assistant's HomeKit Device integration uses).

Two non-obvious requirements, both verified against aiohomekit 3.2.20:
  1. You must pass your OWN AsyncZeroconf instance to Controller().
  2. You must register an AsyncServiceBrowser for BOTH _hap._tcp and _hap._udp
     BEFORE calling controller.async_start(), or it raises
     TransportNotSupportedError("There is no zeroconf browser for ...").
     aiohomekit deliberately does not create the browser itself — Home Assistant
     hands it a shared, already-browsing Zeroconf.

All HomeKit temperatures are Celsius, which is also what Homey expects, so no
unit conversion is needed here (unlike the cloud transport).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiohomekit import Controller
from aiohomekit.model import Accessories
from aiohomekit.model.characteristics import CharacteristicsTypes as C
from aiohomekit.model.services import ServicesTypes as S
from aiohomekit.zeroconf import HAP_TYPE_TCP, HAP_TYPE_UDP, ZeroconfServiceListener
from zeroconf.asyncio import AsyncServiceBrowser, AsyncZeroconf

_LOGGER = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# Shared controller (one Zeroconf per app, refcounted)
# ─────────────────────────────────────────────

class _SharedController:
    """Lazily-built Controller shared by every device in the app."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._refs = 0
        self._azc: AsyncZeroconf | None = None
        self._browser: AsyncServiceBrowser | None = None
        self._controller: Controller | None = None

    async def acquire(self) -> Controller:
        async with self._lock:
            if self._controller is None:
                self._azc = AsyncZeroconf()
                # Must exist before async_start(); see module docstring.
                self._browser = AsyncServiceBrowser(
                    self._azc.zeroconf,
                    [HAP_TYPE_TCP, HAP_TYPE_UDP],
                    listener=ZeroconfServiceListener(),
                )
                controller = Controller(async_zeroconf_instance=self._azc)
                await controller.async_start()
                self._controller = controller
                _LOGGER.debug("HomeKit controller started")
            self._refs += 1
            return self._controller

    async def release(self) -> None:
        async with self._lock:
            self._refs = max(0, self._refs - 1)
            if self._refs > 0 or self._controller is None:
                return
            try:
                await self._controller.async_stop()
            except Exception:  # noqa: BLE001
                pass
            if self._browser is not None:
                try:
                    await self._browser.async_cancel()
                except Exception:  # noqa: BLE001
                    pass
            if self._azc is not None:
                try:
                    await self._azc.async_close()
                except Exception:  # noqa: BLE001
                    pass
            self._controller = None
            self._browser = None
            self._azc = None
            _LOGGER.debug("HomeKit controller stopped")


_shared = _SharedController()


async def acquire_controller() -> Controller:
    return await _shared.acquire()


async def release_controller() -> None:
    await _shared.release()


# ─────────────────────────────────────────────
# Discovery
# ─────────────────────────────────────────────

async def discover_thermostats(timeout: int = 12) -> list[dict[str, Any]]:
    """Return unpaired HomeKit accessories visible on the LAN.

    Category 9 is "Thermostat", but we return everything unpaired and merely
    flag the likely thermostats — a user whose accessory reports an odd category
    can still pick it rather than being silently filtered out.
    """
    controller = await acquire_controller()
    out: list[dict[str, Any]] = []
    try:
        async for discovery in controller.async_discover(timeout=timeout):
            d = discovery.description
            category = getattr(d, "category", None)
            out.append(
                {
                    "id": d.id,
                    "name": getattr(d, "name", None) or d.id,
                    "model": getattr(d, "model", "") or "",
                    "category": str(category),
                    "paired": bool(discovery.paired),
                    "is_thermostat": "THERMOSTAT" in str(category).upper(),
                }
            )
    finally:
        await release_controller()
    return out


async def pair_accessory(device_id: str, pin: str, alias: str) -> dict:
    """Pair with an accessory and return the pairing data to persist.

    `pin` is the 8-digit HomeKit setup code shown on the thermostat's screen.
    Returns the dict accepted by Controller.load_pairing().
    """
    controller = await acquire_controller()
    try:
        discovery = await controller.async_find(device_id, timeout=30)
        if discovery.paired:
            raise AlreadyPairedError(
                "That accessory is already paired to another controller. "
                "Reset HomeKit on the thermostat, then try again."
            )
        finish_pairing = await discovery.async_start_pairing(alias)
        pairing = await finish_pairing(pin)
        return dict(pairing.pairing_data)
    finally:
        await release_controller()


class AlreadyPairedError(Exception):
    """The accessory is already paired to some other controller."""


# ─────────────────────────────────────────────
# Characteristic mapping
# ─────────────────────────────────────────────

# Logical name -> HAP characteristic type. All live on the Thermostat service.
THERMOSTAT_CHARS = {
    "current_temperature": C.TEMPERATURE_CURRENT,
    "target_temperature": C.TEMPERATURE_TARGET,
    "current_mode": C.HEATING_COOLING_CURRENT,
    "target_mode": C.HEATING_COOLING_TARGET,
    "current_humidity": C.RELATIVE_HUMIDITY_CURRENT,
    "cooling_threshold": C.TEMPERATURE_COOLING_THRESHOLD,
    "heating_threshold": C.TEMPERATURE_HEATING_THRESHOLD,
}

# HAP HeatingCoolingState -> Homey thermostat_mode
HAP_MODE_TO_HOMEY = {0: "off", 1: "heat", 2: "cool", 3: "auto"}
HOMEY_MODE_TO_HAP = {"off": 0, "heat": 1, "cool": 2, "auto": 3}

# HAP CurrentHeatingCoolingState -> honeywell_operating_state
HAP_STATE_TO_HOMEY = {0: "idle", 1: "heating", 2: "cooling"}


def map_thermostat(accessories: Accessories) -> dict[str, tuple[int, int]]:
    """Build {logical name: (aid, iid)} for the accessory's Thermostat service.

    Returns an empty dict if the accessory exposes no Thermostat service.
    """
    result: dict[str, tuple[int, int]] = {}
    for accessory in accessories:
        service = accessory.services.first(service_type=S.THERMOSTAT)
        if service is None:
            continue
        by_type = {char.type: char.iid for char in service.characteristics}
        for logical, char_type in THERMOSTAT_CHARS.items():
            iid = by_type.get(_norm(char_type))
            if iid is not None:
                result[logical] = (accessory.aid, iid)
        if result:
            return result
    return result


def map_fan(accessories: Accessories) -> tuple[int, int] | None:
    """(aid, iid) of the fan's target-state characteristic, if the T5 exposes one.

    Many T5 units expose no fan over HomeKit, so the caller must treat a None
    here as "this device simply has no fan control" rather than an error.
    """
    for accessory in accessories:
        for svc_type in (S.FAN_V2, S.FAN):
            service = accessory.services.first(service_type=svc_type)
            if service is None:
                continue
            for char in service.characteristics:
                if char.type in (_norm(C.FAN_STATE_TARGET), _norm(C.ACTIVE)):
                    return (accessory.aid, char.iid)
    return None


def _norm(uuid: str) -> str:
    from aiohomekit.model import normalize_uuid

    return normalize_uuid(uuid)

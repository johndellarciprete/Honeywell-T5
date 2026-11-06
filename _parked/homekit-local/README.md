# Parked: local HomeKit driver

Working code for controlling the thermostat **locally over HomeKit (HAP)** — no Resideo
cloud, no developer API key, push updates instead of polling. Built 2026-07-17,
validated at `publish` level, then parked.

## Why it's parked

**This particular T5 has no HomeKit support.** Its on-device menu offers only Google and
Alexa — no Apple option — and a LAN scan confirmed it never advertises `_hap._tcp`
(an unrelated iSmartGate HomeKit accessory *was* visible from the same machine, so
mDNS/discovery were working fine). The `RCHT8610WF`/`RTH8800WF` family shipped in both
HomeKit and non-HomeKit variants; this unit is the latter.

The code is kept because it is correct and would work as-is against a HomeKit-capable
thermostat (T9/T10, Lyric Round, or a HomeKit-variant T5).

## To un-park

1. `mv thermostat_local ../../drivers/` and `mv homekit.py ../../honeywell/`
2. Re-add to `app.json`: the `thermostat_local` driver block, its flow cards
   (`temperature_changed_local`, `mode_changed_local`, `set_mode_local`,
   `set_fan_mode_local`), and `"aiohomekit"` in `pythonPackages`.
3. Rebuild deps. **`homey app dependencies add` fails on Windows** (uv_cache bind-mount
   permission error) — use the manual builder-image workaround, then
   `scripts/fix_windows_venv.sh`, then `homey app validate`.

## Verified API notes (aiohomekit 3.2.20)

- Pass your OWN `AsyncZeroconf` to `Controller(async_zeroconf_instance=...)` **and**
  register an `AsyncServiceBrowser` for both `HAP_TYPE_TCP` and `HAP_TYPE_UDP` *before*
  `controller.async_start()` — otherwise it raises
  `TransportNotSupportedError("There is no zeroconf browser for _hap._tcp.local.")`.
  aiohomekit never creates the browser itself.
- Pair: `async_find(id)` → `async_start_pairing(alias)` → `finish(pin)`. Persist
  `dict(pairing.pairing_data)`; restore via `controller.load_pairing(alias, data)`.
  PIN must be `XXX-XX-XXX`.
- HAP is always Celsius. `HeatingCoolingState`: 0=off 1=heat 2=cool 3=auto (target only).
- `map_thermostat()` / `map_fan()` in `homekit.py` were unit-verified against a synthetic
  accessory. Many T5s expose no fan service, so the device adds/removes
  `honeywell_fan_mode` at runtime.
- Honeywell quirk: the setup code **printed** on the device often does not work; only the
  code shown on the thermostat's screen after "Reset HomeKit" is valid.

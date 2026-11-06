# Honeywell Home — Homey app

[![Sponsor on GitHub](https://img.shields.io/badge/Sponsor-%E2%9D%A4-ea4aaa?logo=githubsponsors&logoColor=white)](https://github.com/sponsors/johndellarciprete)
[![Donate with PayPal](https://img.shields.io/badge/PayPal-Donate-00457C?logo=paypal&logoColor=white)](https://www.paypal.com/ncp/payment/G2KL8VSBQJUYA)
[![Donate with Venmo](https://img.shields.io/badge/Venmo-Donate-3D95CE?logo=venmo&logoColor=white)](https://venmo.com/John-DellArciprete?txn=pay&amount=10)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

Monitor and control a **Honeywell Home** Wi-Fi thermostat from Homey — the T5,
T6, T9 and T10 all use the same Resideo cloud API and pair through this app.
Works with accounts managed by either the **Honeywell Home / Resideo** app or
the **First Alert** app.

## Features

**Honeywell Thermostat**

| Capability | What it does |
|---|---|
| Target temperature | Dial to set the setpoint; bound to the active mode's heat/cool setpoint |
| Mode | Off / Heat / Cool / Auto |
| Fan | Shows the current fan mode (Auto / On / Circulate) — read-only, see note below |
| Operating state | Sensor: idle / heating / cooling |
| Temperature & humidity | Indoor readings |
| Recent activity | A rolling event log (last 15) on the device's Settings page: mode changes, setpoint changes, heating/cooling started, online/offline |

Long-term value history (temperature, humidity, mode, setpoints, etc.) is also
recorded in Homey **Insights**.

**Flow cards**

- *Actions:* Set mode · Set temperature (Homey built-in) · **Set heating and
  cooling setpoints** (both at once, for Auto mode)
- *Triggers:* Indoor temperature changed · Mode changed

The "Set heating and cooling setpoints" card takes two numbers in your
thermostat's own unit — e.g. heat to `65`, cool to `75` — and applies both,
which is what Auto mode needs (the single target-temperature dial only moves the
active side).

Example flow: *"When everyone has left home → Set mode to Off"* — one card.

## How sign-in works

The T5 has no local API, so this app talks to Resideo's cloud (the same service
behind the Honeywell Home and First Alert apps) using OAuth2.

**Adding a thermostat is a single tap:** **Add device → Honeywell Thermostat → Sign in.**
The Honeywell sign-in page opens inside Homey; you pick **First Alert** or
**Honeywell Home / Resideo** and log in, then Homey returns to the app on its own
(no codes, no keys to enter) and lists your thermostats.

- Only OAuth **tokens** are stored on your Homey; your password is never seen by
  the app.
- Honeywell's own page asks which app your account belongs to — First Alert
  accounts route through Resideo ID (`myid.resideo.com`); picking the matching
  one is what lets the login find your account.
- If you revoke access or change your account, just re-add the device.

## Installation

Installed from source with the Homey CLI — see **[INSTALL.md](INSTALL.md)** for
the full step-by-step guide, including the Windows-specific workarounds.

The short version:

```bash
npm install -g homey
homey login
git clone https://github.com/johndellarciprete/Honeywell-T5.git
cd Honeywell-T5
homey app install
```

Then in the Homey app: **Add device → Honeywell Thermostat → Sign in**, and
pick your thermostat(s).

> **Building from source?** The app ships with shared developer-app credentials
> baked into `honeywell/const.py` (`CLIENT_ID` / `CLIENT_SECRET`). Create your own
> app at [developer.honeywellhome.com](https://developer.honeywellhome.com) with
> Callback URL `https://callback.athom.com/oauth2/callback`, and paste its Consumer
> Key/Secret into `const.py` before installing.

## Notes & limits

- Resideo asks that a thermostat be polled no more than once every 5 minutes;
  this app honors that. Changes made on the thermostat or in the app can
  therefore take up to 5 minutes to appear in Homey. Changes made *from* Homey
  are sent immediately.
- **Fan is read-only.** The fan mode is shown but can't be changed from Homey:
  on First Alert accounts, Resideo's public fan-control endpoint currently
  returns a server-side error. Change the fan in the Honeywell/First Alert app
  for now; this will be re-enabled if Resideo fixes their endpoint.
- Temperatures are stored in °C internally (Homey's convention) and converted at
  the API boundary if the thermostat reports Fahrenheit.
- First Alert accounts use different ID formats (UUID device IDs, bigint
  location IDs); the app handles both transparently.

## Support this project

If this app is useful to you, consider supporting it —
[GitHub Sponsors](https://github.com/sponsors/johndellarciprete) ·
[PayPal](https://www.paypal.com/ncp/payment/G2KL8VSBQJUYA) ·
[Venmo](https://venmo.com/John-DellArciprete?txn=pay&amount=10)

## License

[MIT](LICENSE) — free to use, modify, and redistribute.

## Disclaimer

This is an unofficial, community-built app. It is not affiliated with, endorsed
by, or supported by Honeywell, Resideo, or First Alert. "Honeywell Home" is a
trademark of Honeywell International Inc., used by Resideo under license. Use at
your own risk.

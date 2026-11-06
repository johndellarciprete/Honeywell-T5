# Installation guide

This app is installed from source onto a Homey Pro with the Homey CLI. Expect
10–20 minutes the first time, most of it waiting on Docker.

## What you need

- **Homey Pro** running firmware **13.0.0 or newer** (the Python app runtime).
- **A Honeywell Home / Resideo or First Alert account** with your thermostat(s)
  already set up and online in the app.
- **A Resideo developer app** (build-time only) — free, created once at
  [developer.honeywellhome.com](https://developer.honeywellhome.com). It gives
  you a **Consumer Key** and **Consumer Secret**. Set its **Callback URL** to
  `https://callback.athom.com/oauth2/callback`, then paste the Key/Secret into
  `honeywell/const.py` (`CLIENT_ID` / `CLIENT_SECRET`) before installing. End
  users never see this — they just sign in.
- On your computer:
  - **Node.js 18+** (for the Homey CLI)
  - **Docker Desktop** (the CLI cross-compiles the app's Python dependencies
    inside Linux containers; needed on every OS)
  - **Git**

## 1. Install the Homey CLI and sign in

```bash
npm install -g homey
homey login
homey app list
```

## 2. Get the source

```bash
git clone https://github.com/johndellarciprete/Honeywell-T5.git
cd Honeywell-T5
```

## 3. Build the Python dependencies

With Docker Desktop running:

```bash
homey app dependencies install
```

This pulls Athom's builder images and produces `python_packages/<arch>/.venv`
for both `arm64` and `amd64`. It only needs repeating when the `pythonPackages`
list in `app.json` changes.

> **Windows:** two known issues, both with easy fixes — see
> [Windows workarounds](#windows-workarounds) below before continuing.

## 4. Install onto your Homey

```bash
homey app validate     # optional sanity check
homey app install
```

`homey app install` puts the app on the Homey permanently (it survives
restarts). Use `homey app run` instead for a temporary session with live logs —
the app is removed when you press Ctrl-C.

## 5. Pair the thermostat

1. **Devices → + → Honeywell Thermostat**
2. Tap **Sign in with Honeywell**. The sign-in page opens inside Homey.
3. On Honeywell's page, pick **First Alert** or **Honeywell Home / Resideo**, log
   in, and approve — Homey returns to the app on its own (no codes, no keys).
4. Every thermostat on the account is listed. **Tap one to add it.** Run
   *Add device* again for more.

Your password is never seen by the app; the device keeps OAuth tokens.

## 6. Try it

On the thermostat device, nudge the **target temperature** — the physical
thermostat (and the Honeywell/First Alert app) should update within moments.
Change the mode and confirm the same. (Fan mode is shown but read-only — see the
README's "Notes & limits".)

---

## Windows workarounds

### `uv_cache … Permission denied` during dependencies install

`homey app dependencies install` can fail with:

```
x Failed to download `<package>`
  `-> failed to rename file from /uv_cache/... : Permission denied (os error 13)
```

The CLI bind-mounts the uv cache from NTFS into the Linux build container, where
renames fail. Workaround: run the same builder image yourself with the cache
kept inside the container, writing the venv where the CLI expects it. From Git
Bash, in the app directory:

```bash
PROJ=$(mktemp -d)
cat > "$PROJ/pyproject.toml" <<'EOF'
[project]
name = "com-dellarciprete-honeywellt5"
version = "1.0.0"
requires-python = ">=3.13"
dependencies = ["aiohttp", "certifi"]
EOF
printf '3.13' > "$PROJ/.python-version"

APP=$(pwd -W 2>/dev/null || pwd)
for arch in arm64 amd64; do
  mkdir -p "python_packages/$arch/.venv"
  MSYS_NO_PATHCONV=1 docker run --rm --platform "linux/$arch" \
    -v "$APP/python_packages/$arch/.venv:/.venv" -v "$PROJ:/project" -w /project \
    -e UV_CACHE_DIR=/tmp/uvcache \
    "ghcr.io/athombv/python-homey-app-builder-$arch:latest" \
    uv sync --active --no-dev
done
printf '3.13' > python_packages/.python-version
```

### Dangling symlinks break packaging

The cross-compiled venvs contain Linux symlinks that don't exist on Windows, and
`homey app validate` / `install` throws while copying them. After **every**
dependencies install, run:

```bash
bash scripts/fix_windows_venv.sh
```

It deletes the (unshipped) interpreter symlinks; nothing else changes.

### `getaddrinfo ENOTFOUND …homey.homeylocal.com` during install

The CLI reaches your Homey via a public DNS name that resolves to its LAN IP.
Routers or corporate DNS with **rebind protection** refuse such answers. Fixes,
pick one:

- Add a hosts-file entry (elevated PowerShell), replacing the IP with your
  Homey's:

  ```powershell
  Add-Content C:\Windows\System32\drivers\etc\hosts "`n192.168.1.23`t192-168-1-23.homey.homeylocal.com"
  ipconfig /flushdns
  ```

- Or allowlist `homeylocal.com` in your router's DNS-rebind settings.
- Or disconnect from any corporate VPN and retry.

---

## Troubleshooting

**"No valid email" / login rejects your account** — you almost certainly picked
the wrong app during pairing. First Alert accounts live in Resideo ID and must
use the **First Alert** option (which sends `subSystemId=5` to the login).

**"Sign-in failed"** — if you built from source, confirm the `CLIENT_ID` /
`CLIENT_SECRET` in `honeywell/const.py` are correct and that your Resideo app's
**Callback URL** is exactly `https://callback.athom.com/oauth2/callback`. Then tap
**Sign in** again.

**Authenticated but "No thermostats found"** — you signed in with an account
that doesn't own the thermostat, or (rarely) the developer app needs Resideo to
flag it First-Alert-dedicated. Sign in with the same account the First Alert /
Honeywell Home app uses.

**Changes on the thermostat take a while to appear in Homey** — expected; the
app polls every 5 minutes (Resideo's recommended interval). Commands from Homey
are sent immediately.

**Thermostat shows "Sign-in expired — please re-pair"** — the tokens were
invalidated (e.g. access revoked). Remove and re-add the device.

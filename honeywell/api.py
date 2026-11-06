"""
Honeywell Home / Resideo async API client.

Auth:  OAuth2 authorization-code flow (grant_type=authorization_code), then the
       short-lived (~599 s) access token is renewed with grant_type=refresh_token.
       The token endpoint uses HTTP Basic auth of `consumerKey:consumerSecret`.
REST:  aiohttp + `Authorization: Bearer <access_token>` against api.honeywellhome.com/v2.

The T5 has no local API — everything here is cloud REST.
"""

from __future__ import annotations

import base64
import logging
import re
import ssl
import urllib.parse
from datetime import UTC, datetime, timedelta
from typing import Any

import aiohttp

from .const import (
    ACCOUNT_TYPES,
    API_BASE,
    DEFAULT_ACCOUNT_TYPE,
    DEFAULT_REDIRECT_URI,
    OAUTH_AUTHORIZE_URL,
    OAUTH_TOKEN_URL,
    TOKEN_LIFETIME_FALLBACK,
    TOKEN_REFRESH_MARGIN,
)

_LOGGER = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# TLS / session
# ─────────────────────────────────────────────

def make_ssl_context() -> ssl.SSLContext:
    """SSL context backed by certifi's CA bundle.

    Homey's Python runtime ships no system CA store, so aiohttp's default
    context fails every HTTPS call with:
        CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate
    Always build sessions via `new_session()`.
    """
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001 - fall back to system store if present
        return ssl.create_default_context()


def new_session() -> aiohttp.ClientSession:
    """An aiohttp session that can actually verify TLS on Homey."""
    return aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=make_ssl_context()))


# ─────────────────────────────────────────────
# OAuth2 auth
# ─────────────────────────────────────────────

class OAuth2Auth:
    """OAuth2 authorization-code auth for the Honeywell Home API.

    Consumer Key/Secret are the app's shared developer app (honeywell.const);
    only the per-user OAuth tokens are persisted in the device store (`to_dict`).
    """

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        session: aiohttp.ClientSession,
        redirect_uri: str = DEFAULT_REDIRECT_URI,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._session = session
        self._redirect_uri = redirect_uri

        self.access_token: str | None = None
        self.refresh_token: str | None = None
        self._expiry: datetime | None = None

        # Set to True whenever refresh() rotates the refresh token, so the
        # device layer knows it must persist the new value back to the store.
        self.tokens_rotated = False

    @property
    def client_id(self) -> str:
        return self._client_id

    # ── URLs / headers ─────────────────────────────────────────

    def get_authorize_url(
        self, state: str | None = None, account_type: str = DEFAULT_ACCOUNT_TYPE
    ) -> str:
        params: dict[str, str] = {
            "response_type": "code",
            "client_id": self._client_id,
            "redirect_uri": self._redirect_uri,
        }
        if state:
            params["state"] = state
        # Route the login to the right identity subsystem (First Alert / Resideo).
        # Without this, First Alert (Resideo ID) accounts fail to log in on the
        # legacy Honeywell Home login page. See const.ACCOUNT_TYPES.
        params.update(ACCOUNT_TYPES.get(account_type, ACCOUNT_TYPES[DEFAULT_ACCOUNT_TYPE]))
        return f"{OAUTH_AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"

    def _basic_auth_header(self) -> str:
        raw = f"{self._client_id}:{self._client_secret}".encode()
        return "Basic " + base64.b64encode(raw).decode()

    def _store_token_response(self, data: dict) -> None:
        self.access_token = data["access_token"]
        new_refresh = data.get("refresh_token")
        if new_refresh and new_refresh != self.refresh_token:
            self.refresh_token = new_refresh
            self.tokens_rotated = True
        try:
            ttl = int(data.get("expires_in", TOKEN_LIFETIME_FALLBACK))
        except (TypeError, ValueError):
            ttl = TOKEN_LIFETIME_FALLBACK
        self._expiry = datetime.now(UTC) + timedelta(seconds=ttl)

    # ── Grants ─────────────────────────────────────────────────

    async def exchange_code(self, code: str) -> None:
        """Exchange an authorization code for access + refresh tokens."""
        payload = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self._redirect_uri,
        }
        await self._token_request(payload, context="code exchange")

    async def refresh(self) -> None:
        """Renew the access token using the (rotating) refresh token."""
        if not self.refresh_token:
            raise HoneywellAuthError("No refresh token — re-pair the device.")
        payload = {
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
        }
        await self._token_request(payload, context="token refresh")

    async def _token_request(self, payload: dict, *, context: str) -> None:
        headers = {
            "Authorization": self._basic_auth_header(),
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        }
        try:
            async with self._session.post(
                OAUTH_TOKEN_URL, data=payload, headers=headers
            ) as r:
                data = await r.json(content_type=None)
                if r.status != 200:
                    raise HoneywellAuthError(f"OAuth {context} failed ({r.status}): {data}")
        except aiohttp.ClientError as e:
            raise HoneywellAuthError(f"OAuth {context} network error: {e}") from e
        if not isinstance(data, dict) or "access_token" not in data:
            raise HoneywellAuthError(f"OAuth {context} returned no access token: {data}")
        self._store_token_response(data)
        _LOGGER.debug("OAuth %s OK; access token expires %s", context, self._expiry)

    # ── Lifecycle ──────────────────────────────────────────────

    def _expiring(self) -> bool:
        if not self.access_token or not self._expiry:
            return True
        return datetime.now(UTC) >= (self._expiry - timedelta(seconds=TOKEN_REFRESH_MARGIN))

    async def ensure_valid(self) -> None:
        """Refresh the access token if it is missing or close to expiring."""
        if self._expiring():
            await self.refresh()

    # ── Serialization (device store) ───────────────────────────
    # Only the per-user OAuth tokens are persisted; the client_id/secret are the
    # app's baked-in shared developer app (honeywell.const), not per-device.

    def to_dict(self) -> dict:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
        }


# ─────────────────────────────────────────────
# Honeywell REST client
# ─────────────────────────────────────────────

class HoneywellClient:
    """REST client for locations, thermostat state, and commands."""

    def __init__(self, auth: OAuth2Auth, session: aiohttp.ClientSession) -> None:
        self._auth = auth
        self._session = session

    # ── low-level ──────────────────────────────────────────────

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._auth.access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _url(self, path: str, extra: dict | None = None) -> str:
        params = {"apikey": self._auth.client_id}
        if extra:
            params.update(extra)
        return f"{API_BASE}{path}?{urllib.parse.urlencode(params)}"

    @staticmethod
    def _safe_url(url: str) -> str:
        """URL with the apikey redacted, for logs/errors the user may paste."""
        return re.sub(r"(apikey=)[^&]+", r"\1***", url)

    async def _request(self, method: str, url: str, *, json_body: Any = None) -> Any:
        safe = self._safe_url(url)
        try:
            async with self._session.request(
                method, url, headers=self._headers(), json=json_body
            ) as r:
                text = await r.text()
                if r.status == 401:
                    raise HoneywellAuthError(f"Unauthorized (401): {text[:300]}")
                if r.status == 429:
                    raise HoneywellRateLimitError(f"Rate limited (429): {text[:300]}")
                if r.status >= 400:
                    raise HoneywellAPIError(f"{method} {safe} -> {r.status}: {text[:300]}")
                if not text:
                    return None
                import json as _json

                try:
                    return _json.loads(text)
                except _json.JSONDecodeError:
                    return text
        except aiohttp.ClientError as e:
            raise HoneywellAPIError(f"{method} {safe} network error: {e}") from e

    # ── endpoints ──────────────────────────────────────────────

    async def get_locations(self) -> list[dict]:
        data = await self._request("GET", self._url("/v2/locations"))
        return data if isinstance(data, list) else []

    async def get_thermostat(self, location_id: Any, device_id: str) -> dict:
        url = self._url(
            f"/v2/devices/thermostats/{device_id}", {"locationId": location_id}
        )
        data = await self._request("GET", url)
        return data if isinstance(data, dict) else {}

    async def set_thermostat(
        self, location_id: Any, device_id: str, changeable_values: dict
    ) -> None:
        """POST the (edited) changeableValues object back to the thermostat."""
        url = self._url(
            f"/v2/devices/thermostats/{device_id}", {"locationId": location_id}
        )
        await self._request("POST", url, json_body=changeable_values)

    async def set_fan(self, location_id: Any, device_id: str, mode: str) -> None:
        """Change fan mode. Currently unused: Resideo's First Alert fan endpoint
        returns a 500 (JS-FA-Set-Fan-User-Input crashes server-side), so the fan
        capability is read-only. Re-enable at the driver when Resideo fixes it."""
        url = self._url(
            f"/v2/devices/thermostats/{device_id}/fan", {"locationId": location_id}
        )
        await self._request("POST", url, json_body={"mode": mode})


def iter_thermostats(locations: list[dict]) -> list[tuple[Any, dict]]:
    """Flatten locations into (location_id, device) pairs for thermostats only."""
    out: list[tuple[Any, dict]] = []
    for loc in locations or []:
        loc_id = loc.get("locationID") or loc.get("locationId") or loc.get("id")
        for dev in loc.get("devices") or []:
            dtype = (dev.get("deviceType") or "").lower()
            dclass = (dev.get("deviceClass") or "").lower()
            if "thermostat" in dtype or "thermostat" in dclass or dev.get("changeableValues"):
                out.append((loc_id, dev))
    return out


def device_id_of(dev: dict) -> str:
    return dev.get("deviceID") or dev.get("deviceId") or dev.get("id") or ""


def device_label(dev: dict) -> str:
    name = (
        dev.get("userDefinedDeviceName")
        or dev.get("name")
        or dev.get("deviceModel")
        or device_id_of(dev)
    )
    return str(name)


# ─────────────────────────────────────────────
# Exceptions
# ─────────────────────────────────────────────

class HoneywellError(Exception):
    """Base Honeywell error."""


class HoneywellAuthError(HoneywellError):
    """Authentication / token error."""


class HoneywellAPIError(HoneywellError):
    """API call error."""


class HoneywellRateLimitError(HoneywellAPIError):
    """HTTP 429 — too many requests."""

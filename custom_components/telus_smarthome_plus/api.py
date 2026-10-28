"""Async client for the TELUS SmartHome+ Platform API.

Auth model (HEADLESS username/password — see claude/telus-smarthome-api-map.md):

  1. GET  acr/idpurl                 -> loginUrl (server-driven; the hardcoded
                                        auth-gateway host is never hit directly)
  2. POST <loginUrl> {authdomain,     -> session cookies (from Set-Cookie)
         loginid, password}
  3. GET  acr/token  (Cookie: ...)   -> TokenDto {accessToken, refreshToken}
  4. POST acr/refresh-token {authCode -> new TokenDto
         = refreshToken}

  All Platform calls then carry:
      Authorization: Bearer <access token>
      x-api-key: <PLATFORM_API_KEY>
      x-unified-household-id: <household id>   (when known)
      x-unified-correlation-id: <uuid per request>
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

import aiohttp
import async_timeout

from .const import (
    DEFAULT_AUTH_DOMAIN,
    PATH_ACR_REFRESH,
    PATH_ACR_TOKEN,
    PATH_CLIP_STREAM,
    PATH_CLIPS,
    PATH_DISCOVERY,
    PATH_DEVICE,
    PATH_DEVICES,
    PATH_HOMESTATE,
    PATH_HOUSEHOLDS,
    PATH_IDP_URL,
    PATH_LIVE_CONFIG,
    PATH_LIVE_SESSION_DELETE,
    PATH_MY_PROFILE,
    PATH_REFRESH_STATUS,
    PATH_SEND_COMMANDS,
    PATH_VIDEO_EVENT_SETTINGS,
    PLATFORM_API_KEY,
    PLATFORM_HOST,
    REQUEST_TIMEOUT,
    USER_AGENT,
)

_LOGGER = logging.getLogger(__name__)


class TelusAuthError(Exception):
    """Raised when (re)authentication fails and the user must log in again."""


class TelusApiError(Exception):
    """Raised for non-auth API errors."""


class TelusSmartHomeApi:
    """Thin async wrapper over the TELUS SmartHome+ Platform API."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        username: str | None = None,
        password: str | None = None,
        auth_domain: str = DEFAULT_AUTH_DOMAIN,
        access_token: str | None = None,
        refresh_token: str | None = None,
        profile_id: str | None = None,
        household_id: str | None = None,
    ) -> None:
        self._session = session
        self._username = username
        self._password = password
        self._auth_domain = auth_domain or DEFAULT_AUTH_DOMAIN
        self._access_token = access_token
        self._refresh_token = refresh_token
        self.profile_id = profile_id
        self.household_id = household_id

    # ------------------------------------------------------------------ #
    # token accessors (persisted by __init__.py into the config entry)
    # ------------------------------------------------------------------ #
    @property
    def tokens(self) -> dict[str, str | None]:
        return {
            "access_token": self._access_token,
            "refresh_token": self._refresh_token,
        }

    # ------------------------------------------------------------------ #
    # Headless login: idpurl -> cookies -> token
    # ------------------------------------------------------------------ #
    async def async_login(self) -> None:
        """Full username/password login. Sets access + refresh tokens."""
        if not self._username or not self._password:
            raise TelusAuthError("username/password required for login")

        login_url = await self._get_login_url()
        cookies = await self._post_credentials(login_url)
        await self._token_from_cookies(cookies)

    async def _get_login_url(self) -> str:
        data = await self._request("GET", PATH_IDP_URL, _auth=False)
        # IdpUrlDto { loginUrl, idpParameters, idpType, ... }
        login_url = data.get("loginUrl")
        if not login_url:
            # fall back to the discovery endpoint shape
            disc = await self._request(
                "GET", f"{PATH_DISCOVERY}", _auth=False
            )
            body = disc.get("body") or disc
            login_url = body.get("idpUrl") or body.get("loginUrl")
        if not login_url:
            raise TelusAuthError(f"no loginUrl from idp discovery: {data}")
        return login_url

    async def _post_credentials(self, login_url: str) -> str:
        """POST credentials to the (server-provided) login URL, capture cookies."""
        body = {
            "authdomain": self._auth_domain,
            "loginid": self._username,
            "password": self._password,
        }
        headers = {
            "Accept": "application/json",
            "Accept-Language": "en-CA",
            "x-unified-correlation-id": str(uuid.uuid4()),
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        }
        async with async_timeout.timeout(REQUEST_TIMEOUT):
            async with self._session.post(
                login_url, json=body, headers=headers
            ) as resp:
                text = await resp.text()
                if resp.status not in (200, 201, 204):
                    raise TelusAuthError(
                        f"login failed {resp.status}: {text[:300]}"
                    )
                # Cookies arrive as Set-Cookie headers.
                cookies = resp.headers.getall("Set-Cookie", [])
                cookie_header = "; ".join(c.split(";", 1)[0] for c in cookies)
                if not cookie_header:
                    # some gateways return cookies in the aiohttp jar instead
                    jar = self._session.cookie_jar.filter_cookies(login_url)
                    cookie_header = "; ".join(
                        f"{k}={m.value}" for k, m in jar.items()
                    )
                if not cookie_header:
                    raise TelusAuthError("login returned no cookies")
                return cookie_header

    async def _token_from_cookies(self, cookie_header: str) -> None:
        headers = self._base_headers(auth=False)
        headers["Cookie"] = cookie_header
        data = await self._request(
            "GET", PATH_ACR_TOKEN, headers=headers, _auth=False
        )
        self._store_token(data)

    async def refresh_access_token(self) -> None:
        """POST acr/refresh-token with the refresh token as `authCode`."""
        if not self._refresh_token:
            # No refresh token -> full re-login with stored credentials.
            await self.async_login()
            return
        headers = self._base_headers(auth=False)
        if self.household_id:
            headers["x-unified-household-id"] = self.household_id
        headers["Content-Type"] = "application/json"
        try:
            data = await self._request(
                "POST",
                PATH_ACR_REFRESH,
                json={"authCode": self._refresh_token},
                headers=headers,
                _auth=False,
            )
            self._store_token(data)
        except (TelusAuthError, TelusApiError) as err:
            _LOGGER.info("Refresh failed (%s); re-logging in", err)
            await self.async_login()

    def _store_token(self, data: dict) -> None:
        # GetTokenDto { returnCode, data: TokenDto{accessToken, refreshToken,
        # idToken, tokenType, expiresIn}, errorMessage }
        token = data.get("data") or data.get("body") or data
        if isinstance(token, dict) and "accessToken" in token:
            token = token
        self._access_token = (
            token.get("accessToken") or token.get("access_token")
        )
        self._refresh_token = (
            token.get("refreshToken")
            or token.get("refresh_token")
            or self._refresh_token
        )
        if not self._access_token:
            raise TelusAuthError(f"no access token in response: {data}")

    # ------------------------------------------------------------------ #
    # Bootstrap
    # ------------------------------------------------------------------ #
    async def async_bootstrap(self) -> None:
        """Resolve profile + household from my-profile.

        my-profile returns MyProfileDto:
          { profile: UserProfileDto{ profileId, householdId, emailAddress, ... },
            households: [ { householdId, role, accessStatus } ] }
        The household id is already present, so no second call is needed.
        """
        data = await self._request("GET", PATH_MY_PROFILE)
        root = data
        if isinstance(root, dict) and "profile" not in root:
            root = root.get("data") or root.get("body") or root
        profile = (root.get("profile") if isinstance(root, dict) else None) or {}

        self.profile_id = (
            profile.get("profileId")
            or profile.get("id")
            or profile.get("uuid")
            or self.profile_id
        )
        # Household: prefer the one on the profile, else the embedded list.
        self.household_id = (
            profile.get("householdId")
            or _first_household_id(root)
            or self.household_id
        )

        # Fallback only if we still have no household id.
        if not self.household_id and self.profile_id:
            hh = await self._request(
                "GET", PATH_HOUSEHOLDS.format(profile_id=self.profile_id)
            )
            self.household_id = _first_household_id(hh) or self.household_id

        if not self.household_id:
            raise TelusApiError(
                f"could not resolve household id from profile: "
                f"keys={list(root) if isinstance(root, dict) else type(root)}"
            )

    # ------------------------------------------------------------------ #
    # Devices
    # ------------------------------------------------------------------ #
    async def async_get_devices(self) -> list[dict[str, Any]]:
        data = await self._request(
            "GET", PATH_DEVICES.format(household_id=self.household_id)
        )
        self.last_devices_raw = data  # kept for the one-time diagnostic dump
        return _as_list(data)

    async def async_get_device(self, device_id: str) -> dict[str, Any]:
        return await self._request(
            "GET",
            PATH_DEVICE.format(household_id=self.household_id, device_id=device_id),
        )

    async def async_refresh_status(self, asset_id: str) -> None:
        """Ask the backend to refresh a device's telemetry.

        PlatformDeviceApi.refreshDeviceStatus takes assetId and householdId as
        QUERY params with NO body (a JSON body returns HTTP 200 but returnCode
        17100 "Missing householdId or assetId" -- a silent no-op, which is why
        the old form never refreshed anything). assetId is the device id (the
        camera's "db-..." asset id). The gateway expects the household + profile
        headers the app sends.

        Note: this REQUESTS a refresh. A mains camera updates its telemetry
        promptly; a BATTERY device (e.g. this doorbell) only refreshes
        wifi/temperature on its next wake, so those values can still lag the
        call. The event resources (/aiSensor, /pirSensor) stay fresh regardless.
        """
        headers = self._base_headers(with_household=True)
        if self.profile_id:
            headers["x-unified-profile-id"] = self.profile_id
        await self._request(
            "POST",
            PATH_REFRESH_STATUS,
            params={"assetId": asset_id, "householdId": self.household_id or ""},
            headers=headers,
        )

    def _profile_scoped_headers(self) -> dict[str, str]:
        headers = self._base_headers(with_household=True)
        if self.profile_id:
            headers["x-unified-profile-id"] = self.profile_id
        return headers

    async def async_get_video_event_settings(
        self, device_id: str
    ) -> list[dict[str, Any]]:
        """GET a camera's per-type Detect/Notify recording toggles.

        Returns the settingsGroups list (one group per detection type, each with
        Detect and Notify settings). Empty list if the device has none.
        """
        data = await self._request(
            "GET",
            PATH_VIDEO_EVENT_SETTINGS.format(
                profile_id=self.profile_id or "",
                household_id=self.household_id or "",
                device_id=device_id,
            ),
            headers=self._profile_scoped_headers(),
        )
        body = data.get("body") if isinstance(data, dict) else None
        groups = body.get("settingsGroups") if isinstance(body, dict) else None
        return groups if isinstance(groups, list) else []

    async def async_set_video_event_settings(
        self, device_id: str, settings_groups: list[dict[str, Any]]
    ) -> None:
        """PUT the full settingsGroups array back to change a toggle."""
        await self._request(
            "PUT",
            PATH_VIDEO_EVENT_SETTINGS.format(
                profile_id=self.profile_id or "",
                household_id=self.household_id or "",
                device_id=device_id,
            ),
            json=settings_groups,
            headers=self._profile_scoped_headers(),
        )

    async def async_send_command(
        self,
        asset_id: str,
        capability_identifier: str,
        operation: str,
        *,
        connector_id: str | None = None,
        group_id: str | None = None,
        provider: str | None = None,
    ) -> dict[str, Any]:
        """POST send-commands. One command targets a capability + operation."""
        import time

        command = {
            "traceId": str(uuid.uuid4()),
            "source": "mobile",
            "initTimestamp": int(time.time() * 1000),
            "identifier": asset_id,
            "operation": operation,
            "capabilityIdentifier": capability_identifier,
            "connectorId": connector_id,
            "groupId": group_id,
            "provider": provider,
        }
        body = {
            "body": {
                "householdId": self.household_id,
                "sourceDetails": "home-assistant",
                "commands": [command],
            }
        }
        headers = self._base_headers(with_household=True)
        headers["x-unified-asset-ids"] = asset_id
        headers["content-type"] = "application/json"
        return await self._request(
            "POST", PATH_SEND_COMMANDS, json=body, headers=headers
        )

    async def async_set_homestate(self, mode: str) -> dict[str, Any]:
        return await self._request(
            "PUT",
            PATH_HOMESTATE.format(household_id=self.household_id),
            json={"state": mode},
        )

    # ------------------------------------------------------------------ #
    # Camera live config (temp AWS/KVS creds). Consumed by the KVS bridge.
    # ------------------------------------------------------------------ #
    async def async_get_live_config(self, device_id: str) -> dict[str, Any]:
        headers = self._base_headers(with_household=True)
        headers["x-unified-household-id"] = self.household_id or ""
        return await self._request(
            "GET",
            PATH_LIVE_CONFIG.format(
                household_id=self.household_id, device_id=device_id
            ),
            headers=headers,
        )

    async def async_delete_live_session(self, device_id: str, session_id: str) -> None:
        headers = self._base_headers(with_household=True)
        headers["x-unified-household-id"] = self.household_id or ""
        await self._request(
            "DELETE",
            PATH_LIVE_SESSION_DELETE.format(
                household_id=self.household_id,
                device_id=device_id,
                session_id=session_id,
            ),
            headers=headers,
        )

    # ------------------------------------------------------------------ #
    # Recorded clips (cloud recordings)
    # ------------------------------------------------------------------ #
    async def async_get_clips(
        self, device_id: str | None = None, max_results: int = 50
    ) -> tuple[list[dict[str, Any]], Any]:
        """List recent recorded clips. Returns (clips, raw_response)."""
        path = PATH_CLIPS.format(household_id=self.household_id)
        path += f"&maxResults={max_results}"
        if device_id:
            path += f"&deviceId={device_id}"
        raw = await self._request(
            "GET", path, headers=self._base_headers(with_household=True)
        )
        return _clip_items(raw), raw

    async def async_get_clip_stream_url(self, clip_id: str) -> str | None:
        """Return a playable HLS URL for a recorded clip."""
        path = PATH_CLIP_STREAM.format(
            household_id=self.household_id, clip_id=clip_id
        ) + "?protocol=HLS"
        raw = await self._request(
            "GET", path, headers=self._base_headers(with_household=True)
        )
        return _find_url(raw)

    # ------------------------------------------------------------------ #
    # low-level request with one automatic unified-token refresh on 401
    # ------------------------------------------------------------------ #
    def _base_headers(
        self, *, with_household: bool = False, auth: bool = True
    ) -> dict[str, str]:
        headers = {
            "x-api-key": PLATFORM_API_KEY,
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
            "x-unified-correlation-id": str(uuid.uuid4()),
        }
        if auth and self._access_token:
            headers["Authorization"] = f"Bearer {self._access_token}"
        if with_household and self.household_id:
            headers["x-unified-household-id"] = self.household_id
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any | None = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        _auth: bool = True,
        _retry: bool = True,
    ) -> Any:
        url = path if path.startswith("http") else f"{PLATFORM_HOST}{path}"
        hdrs = headers or self._base_headers(auth=_auth)
        if _auth and "Authorization" not in hdrs:
            if not self._access_token:
                raise TelusAuthError("no access token")
            hdrs["Authorization"] = f"Bearer {self._access_token}"
        try:
            async with async_timeout.timeout(REQUEST_TIMEOUT):
                async with self._session.request(
                    method, url, json=json, params=params, headers=hdrs
                ) as resp:
                    text = await resp.text()
                    # TELUS returns 403 "explicit deny" (not 401) for an expired
                    # token on several endpoints, so refresh on BOTH and retry
                    # once. Without this the integration never recovers and every
                    # entity goes Unavailable until a manual reload.
                    if resp.status in (401, 403) and _auth and _retry:
                        _LOGGER.debug(
                            "%s on %s; refreshing token and retrying once",
                            resp.status, path,
                        )
                        await self.refresh_access_token()
                        # Drop any stale Authorization from caller-supplied
                        # headers so the retry carries the NEW bearer token.
                        retry_headers = headers
                        if headers is not None:
                            retry_headers = {
                                k: v
                                for k, v in headers.items()
                                if k.lower() != "authorization"
                            }
                        return await self._request(
                            method,
                            path,
                            json=json,
                            params=params,
                            headers=retry_headers,
                            _auth=_auth,
                            _retry=False,
                        )
                    if resp.status >= 400:
                        raise TelusApiError(f"{method} {path} -> {resp.status}: {text}")
                    data = await _json(resp, text)
                    # Several endpoints (refreshDeviceStatus, send-commands, ...)
                    # return HTTP 200 with a status envelope; a non-zero
                    # returnCode means the call FAILED despite the 200 -- e.g.
                    # the old json-body refreshDeviceStatus returned status 200
                    # with returnCode 17100 "Missing householdId or assetId".
                    # Surface it instead of silently succeeding.
                    if isinstance(data, dict):
                        rc = data.get("returnCode")
                        if rc not in (None, 0, "0"):
                            raise TelusApiError(
                                f"{method} {path} -> returnCode {rc}: "
                                f"{data.get('errorMessage') or data.get('message') or text}"
                            )
                    return data
        except aiohttp.ClientError as err:
            raise TelusApiError(f"network error on {method} {path}: {err}") from err


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
async def _json(resp: aiohttp.ClientResponse, text: str) -> Any:
    if not text:
        return {}
    try:
        import json as _j

        return _j.loads(text)
    except ValueError:
        return {"_raw": text}


def _as_list(data: Any) -> list[dict]:
    """Platform responses wrap payloads; unwrap the common shapes to a list."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        # Platform wraps payloads as {returnCode, body: {devices: [...]}}.
        for key in ("body", "data", "devices", "items", "results", "payload"):
            val = data.get(key)
            if isinstance(val, list):
                return val
            if isinstance(val, dict):
                nested = _as_list(val)
                if nested:
                    return nested
    return []


def _first_list(data: Any) -> dict | None:
    items = _as_list(data)
    return items[0] if items else None


def _first_household_id(data: Any) -> str | None:
    """Pull the first householdId out of any of the known household shapes:
    MyProfileDto.households[], PlatformHouseHoldDto.households[], or a bare list.
    """
    if not isinstance(data, (dict, list)):
        return None
    candidates: list = []
    if isinstance(data, list):
        candidates = data
    else:
        for key in ("households", "data", "body", "items", "results"):
            val = data.get(key)
            if isinstance(val, list):
                candidates = val
                break
            if isinstance(val, dict) and isinstance(val.get("households"), list):
                candidates = val["households"]
                break
    for item in candidates:
        if isinstance(item, dict):
            hid = item.get("householdId") or item.get("id")
            if hid:
                return hid
        elif isinstance(item, str):
            return item
    return None


def _clip_items(data: Any) -> list[dict]:
    """Unwrap a clip-list payload (shape-tolerant; shape is not yet confirmed)."""
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in ("clips", "items", "results", "recordings", "events",
                    "data", "body"):
            val = data.get(key)
            if isinstance(val, list):
                return [x for x in val if isinstance(x, dict)]
            if isinstance(val, dict):
                nested = _clip_items(val)
                if nested:
                    return nested
    return []


def _find_url(data: Any) -> str | None:
    """Find a stream URL anywhere in a response, preferring an .m3u8."""
    import json as _j
    import re

    blob = _j.dumps(data)
    m = re.search(r'https?://[^\s"\\\']+\.m3u8[^\s"\\\']*', blob)
    if m:
        return m.group(0)
    m = re.search(r'https?://[^\s"\\\']+', blob)
    return m.group(0) if m else None

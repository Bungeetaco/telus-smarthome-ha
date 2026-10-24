"""Save TELUS recorded clips to Home Assistant's media folder as MP4.

Used by: the `telus_smarthome.download_clip` service, the optional auto-save
(options), and the "Play & download" folder in the Media browser.
Clips are remuxed (no re-encode) from TELUS's HLS stream with ffmpeg.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

SAVE_SUBDIR = "telus_clips"
SYNC_INTERVAL = timedelta(minutes=5)
MAX_NEW_PER_SYNC = 5          # don't hammer the camera/cloud in one pass
FFMPEG_TIMEOUT = 600
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _media_root(hass: HomeAssistant) -> str:
    dirs = getattr(hass.config, "media_dirs", None) or {}
    return dirs.get("local") or hass.config.path("media")


def clip_path(hass: HomeAssistant, device_id: str, clip: dict) -> str:
    """Deterministic file path for a clip (so 'already saved' = file exists)."""
    from .media_source import _clip_event, _clip_id, _clip_time

    cid = _clip_id(clip) or "unknown"
    t = _clip_time(clip)
    stamp = t.strftime("%Y%m%d_%H%M%S") if isinstance(t, datetime) else "clip"
    ev = _SAFE.sub("-", _clip_event(clip).lower()).strip("-") or "clip"
    name = f"{stamp}_{ev}_{_SAFE.sub('_', cid)}.mp4"
    return os.path.join(_media_root(hass), SAVE_SUBDIR, _SAFE.sub("_", device_id), name)


def _ffmpeg_bin(hass: HomeAssistant) -> str | None:
    mgr = hass.data.get("ffmpeg")
    binary = getattr(mgr, "binary", None)
    return binary or shutil.which("ffmpeg")


class ClipSaver:
    """Downloads clips; one at a time, never twice."""

    def __init__(self, hass: HomeAssistant, api, coordinator) -> None:
        self.hass = hass
        self.api = api
        self.coordinator = coordinator
        self._lock = asyncio.Lock()

    def cameras(self) -> dict[str, str]:
        from .entity import device_name_of, device_type_of

        return {
            dev_id: device_name_of(dev)
            for dev_id, dev in self.coordinator.data.items()
            if "camera" in device_type_of(dev) or "doorbell" in device_type_of(dev)
        }

    async def async_list(self, device_id: str, max_results: int = 50) -> list[dict]:
        clips, raw = await self.api.async_get_clips(device_id, max_results)
        return [c for c in clips if (c.get("deviceId") in (None, device_id))]

    async def async_save(self, device_id: str, clip: dict) -> str:
        """Ensure the clip exists on disk as MP4 and return its path."""
        from .media_source import _clip_id

        cid = _clip_id(clip)
        if not cid:
            raise ValueError("clip has no id")
        path = clip_path(self.hass, device_id, clip)
        if await self.hass.async_add_executor_job(_nonempty, path):
            return path
        async with self._lock:
            # re-check: another task may have saved it while we waited
            if await self.hass.async_add_executor_job(_nonempty, path):
                return path
            url = await self.api.async_get_clip_stream_url(cid)
            if not url:
                raise RuntimeError("TELUS returned no stream URL for this clip")
            ffmpeg = _ffmpeg_bin(self.hass)
            if not ffmpeg:
                raise RuntimeError("ffmpeg not found on this Home Assistant host")
            await self.hass.async_add_executor_job(
                os.makedirs, os.path.dirname(path), 0o755, True
            )
            tmp = path + ".part"
            proc = await asyncio.create_subprocess_exec(
                ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                "-i", url, "-c", "copy", "-bsf:a", "aac_adtstoasc",
                "-movflags", "+faststart", "-f", "mp4", tmp,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, err = await asyncio.wait_for(proc.communicate(), FFMPEG_TIMEOUT)
            except asyncio.TimeoutError:
                proc.kill()
                await self.hass.async_add_executor_job(_rm, tmp)
                raise RuntimeError("ffmpeg timed out saving clip") from None
            if proc.returncode != 0:
                await self.hass.async_add_executor_job(_rm, tmp)
                msg = (err or b"").decode(errors="replace")[:300]
                raise RuntimeError(f"ffmpeg failed ({proc.returncode}): {msg}")
            await self.hass.async_add_executor_job(os.replace, tmp, path)
            _LOGGER.info("Saved TELUS clip %s -> %s", cid, path)
            return path

    async def async_find_and_save(
        self, device_id: str, clip_id: str | None
    ) -> str:
        """Save a given clip id (or the newest clip) for a camera."""
        from .media_source import _clip_id, _clip_time

        clips = await self.async_list(device_id)
        if clip_id:
            match = next((c for c in clips if _clip_id(c) == clip_id), None)
            if match is None:
                raise ValueError(f"clip {clip_id} not found for {device_id}")
        else:
            if not clips:
                raise ValueError("no clips available")
            floor = datetime.min.replace(tzinfo=timezone.utc)
            match = max(clips, key=lambda c: _clip_time(c) or floor)
        return await self.async_save(device_id, match)

    async def async_sync(self, keep_days: int) -> None:
        """Auto-save recent new clips, then prune old files."""
        saved = 0
        for dev_id in self.cameras():
            try:
                clips = await self.async_list(dev_id)
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Auto-save: could not list clips: %s", err)
                continue
            for c in clips:
                if str(c.get("status", "CAPTURED")).upper() != "CAPTURED":
                    continue
                path = clip_path(self.hass, dev_id, c)
                if await self.hass.async_add_executor_job(_nonempty, path):
                    continue
                if keep_days and _too_old(c, keep_days):
                    continue
                if saved >= MAX_NEW_PER_SYNC:
                    return
                try:
                    await self.async_save(dev_id, c)
                    saved += 1
                except Exception as err:  # noqa: BLE001
                    _LOGGER.warning("Auto-save failed: %s", err)
        if keep_days:
            await self.hass.async_add_executor_job(self._prune, keep_days)

    def _prune(self, keep_days: int) -> None:
        root = os.path.join(_media_root(self.hass), SAVE_SUBDIR)
        cutoff = time.time() - keep_days * 86400
        for dirpath, _dirs, files in os.walk(root):
            for f in files:
                if f.endswith(".mp4"):
                    p = os.path.join(dirpath, f)
                    try:
                        if os.path.getmtime(p) < cutoff:
                            os.remove(p)
                    except OSError:
                        pass


def _too_old(clip: dict, keep_days: int) -> bool:
    from .media_source import _clip_time

    t = _clip_time(clip)
    return bool(
        isinstance(t, datetime)
        and t < datetime.now(timezone.utc) - timedelta(days=keep_days)
    )


def _nonempty(path: str) -> bool:
    try:
        return os.path.getsize(path) > 0
    except OSError:
        return False


def _rm(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


class TelusClipView(HomeAssistantView):
    """GET /api/telus_smarthome/clip/{device_id}/{clip_id}.mp4 -> MP4 file.

    Saves the clip on first request (to /media/telus_clips) then serves it with
    range support, so it plays, seeks, and the browser's Download works.
    """

    url = "/api/telus_smarthome/clip/{device_id}/{clip_id}.mp4"
    name = "api:telus_smarthome:clip"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request, device_id: str, clip_id: str):
        savers = [
            s["clip_saver"]
            for s in (self.hass.data.get(DOMAIN) or {}).values()
            if isinstance(s, dict) and "clip_saver" in s
        ]
        if not savers:
            return web.Response(status=404, text="TELUS SmartHome+ not set up")
        try:
            path = await savers[0].async_find_and_save(device_id, clip_id)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Clip download failed: %s", err)
            return web.Response(status=502, text=f"Could not save clip: {err}")
        return web.FileResponse(
            path,
            headers={
                "Content-Type": "video/mp4",
                "Content-Disposition": f'inline; filename="{os.path.basename(path)}"',
            },
        )

"""Media Source: browse and play TELUS SmartHome+ recorded clips.

Appears in Home Assistant's Media browser as "TELUS SmartHome+" ->
camera -> recent clips. Playback uses the clip's own HLS URL from TELUS, so no
bridge and no extra camera slot is needed.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from homeassistant.components.media_player import BrowseError, MediaClass
from homeassistant.components.media_source import (
    BrowseMediaSource,
    MediaSource,
    MediaSourceItem,
    PlayMedia,
    Unresolvable,
)
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .entity import device_name_of, device_type_of

_LOGGER = logging.getLogger(__name__)

MAX_CLIPS = 50
HLS_MIME = "application/vnd.apple.mpegurl"


async def async_get_media_source(hass: HomeAssistant) -> "TelusMediaSource":
    return TelusMediaSource(hass)


# -- tolerant field readers (clip response shape is not yet confirmed live) --
def _clip_id(c: dict) -> str | None:
    for k in ("clipId", "id", "clip_id", "eventId", "recordingId"):
        if c.get(k):
            return str(c[k])
    return None


def _clip_time(c: dict) -> datetime | None:
    for k in ("startTime", "startTimestamp", "timestamp", "createdAt",
              "eventTime", "time"):
        v = c.get(k)
        if not v:
            continue
        if isinstance(v, (int, float)):
            return datetime.fromtimestamp(v / 1000 if v > 1e12 else v, tz=timezone.utc)
        try:
            return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _clip_event(c: dict) -> str:
    for k in ("eventName", "eventType", "type", "label", "detectionType", "category"):
        if c.get(k):
            return str(c[k]).replace("_", " ").title()
    return "Clip"


def _clip_thumb(c: dict) -> str | None:
    for k in ("thumbnailUrl", "thumbnail", "thumbnailURL", "imageUrl"):
        v = c.get(k)
        if isinstance(v, str) and v.startswith("http"):
            return v
        if isinstance(v, dict):
            for vv in v.values():
                if isinstance(vv, str) and vv.startswith("http"):
                    return vv
    return None


def _clip_device(c: dict) -> str | None:
    for k in ("deviceId", "device_id", "assetId", "cameraId"):
        if c.get(k):
            return str(c[k])
    return None


class TelusMediaSource(MediaSource):
    """Browse TELUS recorded clips."""

    name = "TELUS SmartHome+"

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(DOMAIN)
        self.hass = hass

    def _store(self) -> dict[str, Any]:
        entries = self.hass.data.get(DOMAIN) or {}
        for store in entries.values():
            if isinstance(store, dict) and "api" in store:
                return store
        raise BrowseError("TELUS SmartHome+ is not set up")

    def _cameras(self, store: dict) -> dict[str, str]:
        out = {}
        for dev_id, dev in store["coordinator"].data.items():
            t = device_type_of(dev)
            if "camera" in t or "doorbell" in t:
                out[dev_id] = device_name_of(dev)
        return out

    # ------------------------------------------------------------------ #
    async def async_resolve_media(self, item: MediaSourceItem) -> PlayMedia:
        store = self._store()
        ident = item.identifier or ""
        if "/" not in ident:
            raise Unresolvable("Select a specific clip")
        clip_id = ident.split("/", 1)[1]
        try:
            url = await store["api"].async_get_clip_stream_url(clip_id)
        except Exception as err:  # noqa: BLE001
            raise Unresolvable(f"Could not get clip stream: {err}") from err
        if not url:
            raise Unresolvable("TELUS returned no stream URL for this clip")
        return PlayMedia(url, HLS_MIME)

    async def async_browse_media(self, item: MediaSourceItem) -> BrowseMediaSource:
        store = self._store()
        cameras = self._cameras(store)
        ident = item.identifier or ""

        if not ident:  # root: list cameras
            children = [
                BrowseMediaSource(
                    domain=DOMAIN,
                    identifier=dev_id,
                    media_class=MediaClass.DIRECTORY,
                    media_content_type="",
                    title=name,
                    can_play=False,
                    can_expand=True,
                )
                for dev_id, name in cameras.items()
            ]
            return BrowseMediaSource(
                domain=DOMAIN,
                identifier=None,
                media_class=MediaClass.DIRECTORY,
                media_content_type="",
                title=self.name,
                can_play=False,
                can_expand=True,
                children=children,
                children_media_class=MediaClass.DIRECTORY,
            )

        dev_id = ident.split("/", 1)[0]
        if dev_id not in cameras:
            raise BrowseError(f"Unknown camera: {dev_id}")

        try:
            clips, raw = await store["api"].async_get_clips(dev_id, MAX_CLIPS)
        except Exception as err:  # noqa: BLE001
            raise BrowseError(f"Could not list clips: {err}") from err
        await self._dump_once(raw)

        rows = []
        for c in clips:
            # If TELUS ignored the deviceId filter, drop other cameras' clips.
            cd = _clip_device(c)
            if cd and cd != dev_id:
                continue
            cid = _clip_id(c)
            if not cid:
                continue
            rows.append((_clip_time(c), c, cid))
        rows.sort(key=lambda r: r[0] or datetime.min.replace(tzinfo=timezone.utc),
                  reverse=True)

        children = []
        for t, c, cid in rows:
            when = (
                t.astimezone().strftime("%b %d, %I:%M:%S %p")
                if t else "Unknown time"
            )
            children.append(
                BrowseMediaSource(
                    domain=DOMAIN,
                    identifier=f"{dev_id}/{cid}",
                    media_class=MediaClass.VIDEO,
                    media_content_type=HLS_MIME,
                    title=f"{when} - {_clip_event(c)}",
                    can_play=True,
                    can_expand=False,
                    thumbnail=_clip_thumb(c),
                )
            )
        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=dev_id,
            media_class=MediaClass.DIRECTORY,
            media_content_type="",
            title=cameras[dev_id],
            can_play=False,
            can_expand=True,
            children=children,
            children_media_class=MediaClass.VIDEO,
        )

    async def _dump_once(self, raw: Any) -> None:
        """Write the first raw clip-list response so field names can be checked."""
        path = self.hass.config.path("telus_smarthome_clips.json")

        def _write() -> None:
            import os

            if os.path.exists(path):
                return
            try:
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump(raw, fh, indent=2, default=str)
            except OSError as err:  # noqa: BLE001
                _LOGGER.warning("Could not write clips dump: %s", err)

        await self.hass.async_add_executor_job(_write)

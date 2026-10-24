# Changelog

All notable changes to this project are documented here. This project adheres
to [Semantic Versioning](https://semver.org/). It is **pre-1.0** — expect
breaking changes between minor versions until the API surface stabilizes.

## [0.7.1] — 2026-10-24

### Added
- `AUDIO_GAIN_DB` (default 15): mic boost with a limiter. The camera's mic is
  far-field and quiet; 15-25 dB makes speech clear without letting bangs clip.
  Set in `.env`, then `docker compose up -d`.

### Fixed (docs)
- `LAN_IP` must be set in `.env` (the bridge hands HA `rtsp://<LAN_IP>:8554/...`);
  with it unset HA is told `127.0.0.1` and cannot find the stream.

## [0.7.0] — 2026-10-24

### Added
- **Audio in the live stream.** The bridge now negotiates the camera's Opus audio
  and publishes it as AAC alongside the video (video is still copied, not
  re-encoded). Audio rides a separate pipe that is fed continuously (silence
  when the camera sends none), so it cannot stall video. If a session fails to
  come up with audio, the bridge automatically retries video-only. Turn it off
  with `ENABLE_AUDIO=0`.
- `/health` reports `audio_requested`, `audio_active` and `audio_bytes`.
- **Container limits** in `docker-compose.yml`: memory cap (bridge 768 MB,
  MediaMTX 256 MB, no swap), CPU and process limits, log rotation, `init: true`,
  and a healthcheck — so a leak or runaway process is capped and restarted
  instead of growing without bound.

## [0.6.1] — 2026-10-24

### Added
- **Save folder option** (Configure -> "Save folder for clips"). Point it at any
  folder Home Assistant can write to — e.g. a mounted network share such as
  `/media/nas/cameras` — for both auto-save and "Play & download". The path is
  tested for write access when you save the option. The `download_clip` service
  also accepts a `folder`.
- More logging around clip saving (ffmpeg command/errors).

## [0.6.0] — 2026-10-24

### Added
- **Download clips.** Media browser camera folder now has two views:
  *Play from cloud (stream)* and *Play & download* — the latter saves the clip
  as an MP4 in `media/telus_clips` and plays it from there (seekable, and the
  browser's Download button works).
- **Auto-save** (off by default): Configure -> "Auto-save new recorded clips".
  Checks every 5 minutes, saves new clips as MP4, and prunes files older than
  the "keep days" setting (default 14, 0 = keep forever).
- Service `telus_smarthome.download_clip` (optional `device_id`, `clip_id`;
  defaults to the newest clip).
- Saved clips also appear under *Media -> My media -> telus_clips*.

## [0.5.2] — 2026-10-24

### Fixed
- Clip playback: label the stream `application/x-mpegURL` so Home Assistant's
  player uses its HLS support (browsers could not play it as `vnd.apple.mpegurl`).

## [0.5.1] — 2026-10-23

### Fixed
- Clip list now reads the detection type from the camera's `keywords`
  (e.g. Person + Motion) and shows clip duration, matching the real TELUS response.

## [0.5.0] — 2026-10-23

### Added
- **Recorded clips in the Media browser.** TELUS cloud recordings now appear
  under *Media -> TELUS SmartHome+ -> (camera)* with time, detection type and
  thumbnail, and play via the clip's own HLS stream (no bridge, no extra camera
  slot). Works on dashboards and can be cast to media players.
- The first clip-list response is written to `/config/telus_smarthome_clips.json`
  to help confirm field names across firmware/regions.
- `tools/fetch_clips.py` — standalone script to list/download clips to MP4.

## [0.4.1] — 2026-10-22

### Changed
- **Brand images now ship with the integration** at
  `custom_components/telus_smarthome/brand/` (icon + logo). Home Assistant
  2026.3+ serves these locally and they override the brands CDN, so the icon
  shows on the device/integration card with no submission to
  home-assistant/brands and no extra config.

## [0.4.0] — 2026-10-17

### Added
- **AI Sensor** — surfaces the camera's on-board object classification
  (Person / Vehicle / Animal / Package / Motion) as an entity, with
  confidence, timestamp, and bounding-box attributes.
- **Self-healing stream bridge** — the KVS→RTSP bridge now detects when the
  camera drops a live session (≈10-minute cap) and auto-reconnects within a
  few seconds, with a watchdog and a `/health` endpoint reporting per-session
  state. No more manual reloads to recover video.
- `hacs.json` — installable as a HACS custom repository.
- Project branding — logo and app icon in [`brand/`](brand/).

### Fixed
- **Token refresh on 403.** TELUS returns `403 "explicit deny"` (not `401`)
  for an expired token on several endpoints; the client now refreshes and
  retries on both, so entities no longer go *Unavailable* every few minutes.

## [0.3.0] — 2026-10-16

### Added
- **Live video** via a GStreamer `webrtcbin` → RTSP bridge (replacing an
  earlier aiortc attempt that never received media). Publishes to MediaMTX for
  Home Assistant and HomeKit.
- Motion, connectivity, Wi-Fi signal, and device-temperature sensors.

## [0.2.0] — 2026-10-16

### Added
- Headless email/password login (idpurl → credentials → token, with refresh).
- Device discovery, camera entity, and control scaffolding (switch/light/lock).

## [0.1.0] — 2026-10-16

### Added
- Initial reverse-engineering of the TELUS SmartHome+ Platform API and project
  scaffolding.

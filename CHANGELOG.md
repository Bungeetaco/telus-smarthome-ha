# Changelog

All notable changes to this project are documented here. This project adheres
to [Semantic Versioning](https://semver.org/). It is **pre-1.0** — expect
breaking changes between minor versions until the API surface stabilizes.

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

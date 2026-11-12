# Backlog — findings not yet applied

Source: four independent reviews (correctness, security, operability, media
pipeline) of `gst_bridge/` and the bridge-facing parts of the integration on
2026-11-12, after commit c383a8c. Everything verified-and-cheap went into the
same-day follow-up commit; this file records what was deliberately left for a
decision, with the evidence, so "not implemented" never reads as "not wanted".

Severity is against the real deployment: HA, go2rtc, Frigate and the bridge on
one host (10.13.38.2) that sits on the IoT VLAN.

## A. Deployment / configuration decisions (operator)

| # | Item | Evidence | Proposed change | Effort |
|---|------|----------|-----------------|--------|
| A1 | **DONE 2026-11-12** (override: `MTX_RTSPADDRESS: 127.0.0.1:8556`, read/publish base and HA go2rtc.yaml on loopback). ~~MediaMTX accepts anonymous RTSP publish and read on every interface of the IoT VLAN.~~ Any device on 10.13.38.0/24 can watch the doorbell or ANNOUNCE its own H.264 to `telus_db-…` and replace the feed HA/Frigate consume. | `ss` shows `*:8556`; anonymous DESCRIBE from the LAN returns the SDP; MediaMTX 1.9.3 default `authInternalUsers` = any/any. | In the local compose override: `MTX_RTSPADDRESS: 127.0.0.1:8556`, `RTSP_PUBLISH_BASE`/`RTSP_READ_BASE: rtsp://127.0.0.1:8556`, and HA's `go2rtc.yaml` doorbell source → `127.0.0.1`. Every consumer is host-network on this VM, so no functional cost. Alternative if LAN reach is ever needed: `MTX_AUTHINTERNALUSERS` with a publish user embedded in the bridge's publish URL. | S |
| A2 | **DONE 2026-11-12** (`BRIDGE_HOST=127.0.0.1`, integration bridge URL `http://127.0.0.1:8787`; allow-list/shared secret still open). ~~Bridge control API on 0.0.0.0:8787.~~ A LAN caller can stop the live session or point the bridge at its own signaling/TURN server (reachability oracle into BungeeNet from the one VM allowed through the firewall). | `handle_start` accepts any `wss_endpoint`/`https_endpoint`. | Set `BRIDGE_HOST=127.0.0.1` in the override (knob exists since 2026-11-12) and change the integration's Bridge URL to `http://127.0.0.1:8787`. Optional hardening: shared-secret header, endpoint allow-list `^wss://[a-z0-9.-]+\.kinesisvideo\.<region>\.amazonaws\.com$`. | S |
| A3 | **DONE 2026-11-12** (compose: uid 65534, read-only, cap_drop ALL, no-new-privileges, /tmp tmpfs, for both containers). ~~Container runs as root with host networking and parses attacker-selectable SDP/RTP/H.264/Opus in C libraries.** | `docker exec id` → uid 0; no `user:`, no `cap_drop`, no `read_only`. | compose: `user: "65534:65534"`, `read_only: true`, `tmpfs: [/tmp]`, `cap_drop: [ALL]`, `security_opt: [no-new-privileges:true]`, `GST_REGISTRY=/tmp/gst-registry.bin HOME=/tmp`. Port 8787 > 1024 so nothing needs privilege. | S |
| A4 | **DONE 2026-11-12** (files deleted, dump code removed in 0.13.0). ~~Leftover diagnostic dumps in `/config`.~~ `telus_smarthome_plus_livestream.json` (0644) holds a now-expired AWS temporary credential set + channel ARN; `_dump.json`/`_clips.json` hold household/profile IDs. They ride along in the nightly B2 backup. | `__init__.py` `_dump_livestream`/`_dump_devices`, `media_source.py` `_dump_once` always run once. | Delete the three files; remove the dump calls or gate them behind a debug option. | S |
| A5 | **DONE 2026-11-12** (`chmod 600`; watch for HA rewriting it). `.storage/core.config_entries` was 0644 while `.storage/auth` is 0600; it holds the TELUS password and tokens (HA-standard storage, but the mode is an anomaly). | `ls -l` on the HA config dir. | `chmod 600`; watch whether HA rewrites it 0644 and find the writer if so. | S |

## B. Media pipeline (design change, needs a maintenance window)

| # | Item | Evidence | Options | Effort |
|---|------|----------|---------|--------|
| B1 | **DONE 2026-11-12** (option (a): matroskamux in-graph, Opus kept encoded into the mux, ffmpeg `-f matroska`; plus arrival-time RTP re-stamping and a per-track smoothing clock because the camera's RTP clock is frame-count based and its Wi-Fi delivery is bursty; measured 19.3 PTS-s = 19.5 wall-s, spacing 20-50 ms, 0 ffmpeg warnings). ~~Video timestamps are fabricated at a fixed 19 fps.~~ A raw Annex-B pipe carries no PTS; ffmpeg 5.1 stamps every frame from the SPS VUI (`time_scale=38000, num_units_in_tick=1000`) and the `-framerate 30` flag is overridden. The camera really delivers 14–30 fps, so recordings run fast or slow against their (wall-paced) audio, Frigate's 10 s segments hold 10·real_fps/19 s of playback, and ffmpeg's interleaver held one stream back by up to 10 s (now bounded to 1 s by `-max_interleave_delta`). | `ffprobe -show_packets` on the published RTSP: 150 of 151 PTS deltas = 0.052622 s while the arrival rate measured 22.9 fps. `-use_wallclock_as_timestamps 1` was tried 2026-11-12 and rejected: with the 1 MiB pipe ffmpeg drains bursts and stamps a whole burst identically (storm of "Non-monotonous DTS"). | **(a)** Mux in GStreamer and carry the jitterbuffer PTS: `matroskamux streamable=true ! queue leaky=downstream ! fdsink` into ffmpeg `-f matroska`, audio as raw PCM or `opusparse` into the same mux (removes the second pipe and the Python audio writer; startup needs the audio pad before the header, or an `audiotestsrc wave=silence ! audiomixer` guarantee). **(b)** Drop ffmpeg: `apt-get install gstreamer1.0-rtsp` and publish with `rtspclientsink` (Opus end-to-end, or `voaacenc` for AAC); removes the child process and the kill/EOF machinery. **(c)** Spike: HA's go2rtc 1.9.x has `webrtc:` source `#format=kinesis` (used for Ring); the bridge would shrink to a URL signer. Add a timestamp-contract test first (see tests/). | M / M-L / L |
| B2 | **OBSOLETE 2026-11-12** (fdsink + per-track leaky queues in C replaced the Python write path). ~~Dedicated writer thread with a GOP-aware bounded deque~~ instead of writing on the depayloader's streaming thread. Only worth it if B1(a)/(b) are not done; with them the `queue leaky=downstream` does it in C. | `_on_sample` runs on the GStreamer streaming thread; a stall of `PIPE_WRITE_TIMEOUT_SEC` back-pressures the jitterbuffer. Mitigated 2026-11-12 (1 MiB pipe, 0.5 s bound, IDR wait after a drop). | — | M |

## C. Observability / UX (integration side)

| # | Item | Proposed change | Effort |
|---|------|-----------------|--------|
| C1 | **DONE 2026-11-12** (0.13.0: `binary_sensor.<camera>_bridge_stream` + `sensor.<camera>_bridge_reconnects`). ~~Nothing in Home Assistant reflects bridge health.~~ Leaks and a 27 h outage were invisible except in Frigate recordings and docker logs. | Poll `{bridge_url}/health` every 30 s from the coordinator: `binary_sensor.<camera>_bridge_stream` (device_class connectivity; on = reachable and this device's session `healthy`) with attributes `secs_since_frame, reconnect_attempts, reconnects_total, gave_up, nack_negotiated, audio_fallback, video_drops, audio_drops, fds, ffmpeg_children, wedged_sessions, version`; `sensor.<camera>_bridge_reconnects` (total_increasing). All fields exist in `/health` since 2026-11-12. | M |
| C2 | **DONE 2026-11-12** (0.13.0). `camera.py` ignored `started=false` in the `/start` response and its error lines carry no device id or bridge URL. | Warn when `started` is false (`reused` too); prefix the three error logs with device_id and bridge_url; document the 45 s HA timeout vs the bridge's 12 s wait. | S |
| C3 | **DONE 2026-11-12** (flows probe `/health`). README part still open. Bridge URL was never validated in the config flow; `LAN_IP` unset is the #1 support issue (CHANGELOG 0.7.1) and the compose override's semantics are undocumented. | `GET {bridge_url}/health` in both flows with a clear error string; README paragraph on `docker-compose.override.yml` (auto-merged, gitignored, `docker compose config` shows the result) and on moving MediaMTX off 8554. (Bridge-side startup warning for a loopback `RTSP_READ_BASE` added 2026-11-12.) | S |
| C4 | Audio fallback (video-only after `AUDIO_FALLBACK_AFTER` failed reconnects, or after `PAD_SETTLE_MS` without an audio pad) changes MediaMTX's track count under running readers, so go2rtc/Frigate must reconnect and recordings silently lose sound. | Log it at WARNING (done) and surface `audio_fallback` in HA (C1); consider keeping a silent AAC track in video-only mode so the track count never changes. | S/M |
| C5 | README still lacks a "Verifying the bridge" section: a healthy `/health` body annotated, the three failure signatures (`secs_since_frame` > 8, `gave_up`, `wedged_sessions` > 0 / `ffmpeg_children` > sessions, MediaMTX "no one is publishing"), the `answer rtcp-fb … nack, nack pli` line, the `SIGUSR1` stack dump, and "Docker marks unhealthy but never restarts; pair with a watchdog". Tuning table should list `ENABLE_AUDIO, AUDIO_GAIN_DB, AUDIO_FALLBACK_AFTER, WATCHDOG_INTERVAL_SEC, RTSP_PUBLISH_BASE, RTSP_READ_BASE, BRIDGE_PORT, BRIDGE_HOST, MAX_SESSIONS`. `docs/ARCHITECTURE.md:100` still says audio is not muxed. | Docs only. | S |

## D. Image / build

| # | Item | Proposed change | Effort |
|---|------|-----------------|--------|
| D1 | **PARTLY DONE 2026-11-12**: base pinned by digest, `-libav` dropped, but the image stays ~1.1 GB because ffmpeg needs the libav libraries; only dropping ffmpeg (B1 option b) shrinks it. Image is 1.14 GB; `gstreamer1.0-libav` and `-plugins-ugly` are not in the data path (`x264enc` from -ugly is used only by the test suite), Mesa/VA-API/GTK are pulled transitively and unused. Base image unpinned. | Drop `-libav`; move `-ugly` to a test stage or keep it for tests only; `FROM debian:bookworm-slim@sha256:<digest>` (pin by digest, not per-package versions, so security point releases still apply). ~400–500 MB saved. | S |
| D2 | **DONE 2026-11-12** (60 s). Compose `healthcheck.start_period: 20s` is shorter than `STARTUP_GRACE_SEC=25`; `/health` can now return 503. | `start_period: 60s`. | S |

## E. Integration code hygiene (low)

| # | Item | Proposed change |
|---|------|-----------------|
| E1 | **DONE 2026-11-12** (0.13.0). `download_clip` took a free-form `folder`; `_prune` deletes every `*.mp4` older than `keep_days` under the save folder (a save folder of `/media` would prune unrelated files). | Constrain under the media root with `os.path.commonpath`; prune only files matching the bridge's own `^\d{8}_\d{6}_.*\.mp4$`. |
| E2 | **PARTLY DONE 2026-11-12** (`file` dropped; `_find_url` unchanged until the response shape is confirmed). `clips.py` remux `-protocol_whitelist` included `file`; `api.py._find_url` takes the first http(s) URL anywhere in the JSON. | Drop `file`; read the known field once the response shape is confirmed. |
| E3 | **DONE 2026-11-12**. `TelusAuthError`/`TelusApiError` embedded full response bodies in exceptions that are logged at WARNING. | Log `list(data)` / `text[:200]`. |
| E4 | **DONE 2026-11-12**. `camera.py:3` docstring said `kvs_bridge/` (directory is `gst_bridge/`). | Fix the path. |

## Ruled out (checked)

- TLS verification is not bypassed anywhere (websocket-client defaults, boto3 default verify, HA's shared session).
- No credentials in bridge logs or `/health`; signed WSS URL and TURN URIs are never logged.
- Hard-coded vendor gateway key / client id / User-Agent are the mobile app's public values, not operator secrets.
- Frigate's `connection_quality: unusable` on 2026-11-12 came from HA restarts and bridge redeploys, not bridge stalls.

## Operational notes learned while applying A1/A2 (2026-11-12)

- go2rtc's `PUT /api/streams` **persists the new source into go2rtc.yaml** and
  rejects sources containing spaces (the ffmpeg `#raw=-vf crop=...` form); the
  only runtime path for such a stream is editing the file and restarting the
  process. The `/config/go2rtc-1.9.9` process that owns 1984/9554 is launched
  and respawned by the **WebRTC custom integration** (custom_components/webrtc),
  not by HA core's go2rtc component (whose bundled binary fails to bind 1984 and
  sits idle on 18554/18555). Killing the WebRTC one is safe: it respawns in ~2 s.
- Integration code changes need a **full HA restart**; a config-entry reload
  re-runs the already imported modules.

## Observed camera behaviour (2026-11-12, for whoever touches timing next)

- RTP timestamps increase by 90000/19 per frame regardless of the real frame
  rate (14-30 fps by light); the SPS VUI claims a fixed 19 fps too.
- Wi-Fi delivery is bursty: tcpdump of the doorbell's flow (10.13.38.212 ->
  bridge, direct LAN candidate) shows 2-6 frames within 2 ms then 200-500 ms
  gaps, keyframes as ~13-packet bursts every second. rtpjitterbuffer in slave
  mode cannot cope with the clock-rate error (re-syncs every ~3 s).
- The Opus RTP clock is sample-accurate; it is still smoothed the same way so
  both tracks share one timebase.
- Residual: after a camera-side stall of ~1 s the smoothing clock catches up
  over ~8 frames at ~110 ms spacing (brief slow motion). Acceptable; a faster
  gain would re-introduce burst jitter.

## Incident 2026-11-12 08:04Z: doorbell offline (camera side)

- Session published 06:15:58Z streamed continuously for 108 min, then MediaMTX
  saw the publisher go silent (i/o timeout 08:04:32Z). From then on: signaling
  reachable (offers sent, 43 of 806 answered), ICE never completed, no media.
- Camera facts: ARP for 10.13.38.212 failed at 14:05Z; later pings 10 % loss
  with 60-2000 ms RTT on the LAN; cloud connectivity flipping every 30-60 s;
  Wi-Fi RSSI (-61) and temperature (46 C) last reported 05:14Z. The living
  room camera (other bridge) was fine throughout: not a host/network issue.
- Resolution: the doorbell is MAINS powered (owner), so not a battery issue.
  A power cycle at ~15:17Z brought it back: pings 5 ms / 0 % loss, session up
  at 15:19:03Z, Frigate at 5 fps by 15:20Z. Cause was the camera's Wi-Fi link
  (RSSI -61 dBm at its last report, 1-2 s LAN round trips while degraded). If
  it repeats: check the access point it joins, channel/interference, doorbell
  firmware. Recovery after the camera returns takes up to RESTART_BACKOFF_SEC
  (120 s) because the bridge had given up.

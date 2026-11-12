# Changelog

All notable changes to this project are documented here. This project adheres
to [Semantic Versioning](https://semver.org/). It is **pre-1.0** — expect
breaking changes between minor versions until the API surface stabilizes.

## [Unreleased]

### Fixed (gst_bridge)
- **A camera that never answers was dialled every 27 s, forever.** Regression
  from the credential-refresh change: the `/start` reuse path reset the
  reconnect counter on every Home Assistant call, so the watchdog's "give up
  after 6" never held. With the doorbell off Wi-Fi from 08:04 UTC on
  2026-11-12 that meant 806 SDP offers in six hours, and the camera's cloud
  connectivity flapped on/off every 30-60 s in step with the attempts (each
  viewer request wakes the camera). Now the counter is reset only when video is
  actually flowing; a connecting session is reused untouched; after giving up,
  one fresh attempt is made per `RESTART_BACKOFF_SEC` (120 s), with audio,
  inheriting the give-up state so the watchdog adds nothing. `/start` answers
  `backoff: true` while waiting. Observed afterwards: one offer per ~3 min and
  the connectivity flapping slowed to match.

### Added / Changed (gst_bridge) — backlog sweep 2026-11-12
- **Audio is continuous and always present** (BACKLOG B3, C4). `mpegtsmux`
  (a GstAggregator with no next-time hook; `force-live` only exists from
  GStreamer 1.24) emits nothing until every pad has a buffer, so a camera
  pausing its audio stalled the video and the watchdog restarted a healthy
  session after 8 s; and a session without audio changed MediaMTX's track
  count under its readers. The camera's Opus is now decoded into a live
  `audiomixer` together with a silence source and re-encoded (`opusenc`),
  so there is always exactly one audio track and an audio pause is silence.
  The video-only fallback and the "audio pad never arrived" case produce the
  same two-track stream; a late audio pad simply joins the mix. New knob
  `AUDIO_MIX_LATENCY_MS` (1000). The pre-keyframe audio gate moved to the mix
  output. `/health` gains `audio_source` (`camera`/`silence`) and
  `audio_rms_db` (camera vs mix, from `level` meters). Verified live: camera
  and mix within 1 dB, Frigate 5 fps; a test closes a valve on the audio for
  3 s and asserts the mux keeps emitting.
- **Control API hardening** (BACKLOG A2). `/start` refuses signaling
  endpoints outside `ENDPOINT_ALLOW_RE` (AWS Kinesis Video `wss`/`https`
  hosts), so the bridge cannot be used as a reachability oracle; with
  `BRIDGE_TOKEN` set, `/session/*` requires the `X-Bridge-Token` header
  (constant-time compare; `/health` stays open for the Docker healthcheck).
  Compose and `.env.example` carry the knob; the integration sends the
  token from its new "bridge token" option.
- **Test build stage** (BACKLOG D1). `gstreamer1.0-plugins-ugly` (x264enc,
  tests only) moved to a `test` stage; the runtime image has no H.264
  encoder. Measured: 1137 MB runtime vs 1141 MB test image, i.e. the
  package was never the size problem; ffmpeg's libav libraries are, and
  only dropping ffmpeg (B1 option b) would change that.

### Added / Changed (integration 0.14.0)
- Options flow: **bridge token**, sent as `X-Bridge-Token` on `/session/*`
  (and on `/health`); a 401 from the bridge is logged with the fix.
- `binary_sensor.<camera>_bridge_stream` attributes gain `audio_source`,
  `pre_keyframe_dropped`, `pre_keyframe_audio_dropped`, `last_au_age_s`.
- Clip stream URL is read from the confirmed response field
  `body.streamingUrl` (BACKLOG E2; the regex sweep stays as fallback).

### Operations (outside this repo, recorded here because the backlog lived here)
- BACKLOG B4: the host's 10-minute container watchdog (`ha-vm-maintenance`)
  restarts Frigate when a camera reads 0 fps for 5 min while its source still
  answers ffprobe (the wedged-capture state after a stream rotation).
- BACKLOG A5: the same run re-tightens `.storage/core.config_entries` to
  0600; Home Assistant rewrites it 0644 on every save (its Store is not
  private), so a one-time chmod did not hold.

### Fixed (gst_bridge) — MPEG-TS instead of Matroska
- **A mid-session H.264 parameter change ended the session.** The Matroska
  path (below) died with `gst_matroska_mux_video_pad_setcaps: Could not
  multiplex stream` the moment the camera sent a new SPS (day/night or
  bitrate switch): a streamable Matroska header cannot take new codec data.
  That ended a 95-minute session at 16:58 UTC on 2026-11-12 and, through the
  resulting rotation, left Frigate's doorbell capture process dead (the
  second "doorbell offline" of the day). The container is now MPEG-TS
  (`mpegtsmux`, H.264 as byte-stream with SPS/PPS in-band via `h264parse
  config-interval=-1`, Opus as-is; `ffmpeg -f mpegts -i pipe:0`). Parameter
  changes flow through; a test switches the resolution mid-stream and asserts
  the output decodes at both sizes (the same test fails on the Matroska
  chain).
- Four things that only showed up live, each with a regression test:
  - `mpegtsmux` pushes buffer *lists*; the fdsink heartbeat probe was
    BUFFER-only, never fired, and the watchdog killed every healthy session
    as "stale" after 8 s (`OUT_PROBE_TYPE` now includes `BUFFER_LIST`).
  - `mpegtsmux` request pads are named by PID; `sink_0`/`sink_1` are rejected
    (PIDs below 0x40 are reserved), so the branches link to `sink_64`/`sink_65`.
  - The SDP carries no `sprop-parameter-sets` and the camera's first IDR did
    not always bring its SPS/PPS, so the TS opened on an undecodable keyframe
    and ffmpeg's probe ended with "Could not find codec parameters ...
    unspecified size". The depayloader now outputs AVC and `h264parse`
    converts back to byte-stream, putting SPS/PPS in front of every keyframe
    including the first (`test_stream_opens_with_sps_pps_even_without_sprop`).
  - Audio that arrived while the depayloader was still waiting for the first
    keyframe came out of the mux first and alone (DTS order), so ffmpeg's
    probe window closed on audio only. Audio is now dropped until the first
    video keyframe has passed (`pre_keyframe_audio_dropped` in `/health`);
    video is gated on a keyframe too, with an upstream `GstForceKeyUnit`
    (PLI) request once per second while waiting (`pre_keyframe_dropped`).
- ffmpeg's TS probe always runs the full `-analyzeduration` (MPEG-TS has no
  header), so it *is* the start-up delay: 1 s now (`-probesize 2000000`),
  measured 1-2 s from "publishing" to MediaMTX "is publishing" (was 4 s with
  the 4 s window tried first).
- `/health` per session now also reports `last_au_age_s` per track (which
  track starved tells a camera pause from a mux stall), the two pre-keyframe
  drop counters, and the bridge logs ffmpeg's exit code (`rc=255` = killed by
  us, `rc=1` = ffmpeg gave up on its own, `rc=0` = EOF on its stdin).
- Guarded `_running_time()` against probes that fire while the pipeline is
  already torn down (a burst of `AttributeError: 'NoneType'` tracebacks per
  teardown).

### Fixed (gst_bridge) — real timestamps end to end
- **Video was published at a fixed 19 fps clock regardless of the camera's real
  14-30 fps**, so Frigate recordings ran fast or slow against their audio and a
  10 s segment held 10·fps/19 s of playback. The bridge wrote bare Annex-B
  H.264 into ffmpeg, which can carry no timestamps; ffmpeg then stamped every
  frame from the SPS VUI timing. The media path is now muxed in GStreamer (Matroska at first, MPEG-TS
  since the entry above):
  `rtph264depay ! h264parse ! queue ! matroskamux` with the camera's Opus
  (`rtpopusdepay ! opusparse ! queue ! matroskamux`) in the same Matroska
  stream, written by `fdsink` straight into `ffmpeg -f matroska -i pipe:0`,
  which copies the video, decodes/boosts/AAC-encodes the audio and publishes
  RTSP. Both tracks carry the jitterbuffer's PTS, so A/V stay in sync and
  ffmpeg's interleaver no longer holds a stream back. Measured on the published
  stream: reading 15 PTS-seconds takes 15 wall-seconds (was ~12), PTS spacing
  follows the real frame rate (33 ms at ~28 fps).
- Removed with it: the second (PCM) pipe, the Python audio writer and silence
  pacer, the bounded `_write_pipe`, the IDR-wait after a drop (`fdsink` writes
  whole clusters; stalls are isolated by per-track `queue leaky=downstream`
  and surface as `video_drops`/`audio_drops` via the queues' `overrun`), and
  `PIPE_WRITE_TIMEOUT_SEC`. New knob `PAD_SETTLE_MS` (1500): how long the muxer
  waits for the audio pad before publishing video-only. The heartbeat is a
  probe on `fdsink`'s sink pad (what ffmpeg actually receives).
- Every webrtcbin src pad is linked inside its own pad-added handler, because
  the first RTP buffer follows pad-added by ~1 ms and a push on an unlinked
  pad is a fatal not-linked error for the source; audio and video pad-added
  arrive on different threads, so the handler is serialised. The muxer's
  audio request pad is created up front and released on the settle timeout
  if the track never arrives (`-map 0:a?` keeps ffmpeg happy without audio).
- `aresample=async=1` on the audio filter: the camera's Opus RTP clock jumps
  backwards by ~100 ms now and then, which showed up as "Non-monotonous DTS"
  on the AAC output once the real timestamps were carried through.
- Teardown clears the chain and pad references too (fd count after a
  reconnect is identical to a fresh start again).
- **Arrival-time stamping + smoothing clock.** With real timestamps carried
  through, the camera's own clock turned out to be unusable: its RTP
  timestamps are frame-count based at a nominal 19 fps (like its SPS VUI) while
  it captures 14-30 fps, so RTP time runs up to 1.4x real time; rtpjitterbuffer
  (slave mode) paces to that clock and re-syncs every few seconds (forward
  bursts, backward jumps, a "Non-monotonous DTS" every ~3 s). And on the wire
  (tcpdump of the doorbell's own UDP flow) the camera transmits in bursts over
  Wi-Fi: several frames within 2 ms, then 200-500 ms of silence, while
  capturing steadily. The bridge therefore (1) records each RTP packet's
  arrival running time at the jitterbuffer's sink pad and writes it back as
  the packet PTS at its src pad (keyed by sequence number, first arrival wins
  so retransmissions do not count), and (2) per track replaces that bursty
  arrival time with a phase-locked clock (`_Pll`): constant spacing equal to the
  mean arrival interval over the last 3 s of arrivals (time-based, so a
  post-stall burst of 60 frames cannot collapse it), pulled toward arrival with
  gain 0.05 so it never drifts, each step floored at a third of the nominal
  interval (no fast-forward) and capped at 4x (gradual catch-up), monotonic by
  construction; a buffer that somehow reaches the muxer without a PTS is given
  the next tick instead of a duplicate timecode (`au_without_pts` in /health). Measured on the published
  stream: 19.3 PTS-seconds read in 19.5 wall-seconds, video spacing 20-50 ms
  (median 33 ms), audio exactly 20 ms, zero ffmpeg timestamp warnings, zero
  queue drops. `/health` gains `rtp_restamped` / `rtp_unstamped` and
  `arrival_hist` (inter-arrival histograms at nicesrc and at the jitterbuffer
  input, the diagnostics that located the burstiness). Per-track queues hold
  5 s so ffmpeg's start-up no longer costs frames. Tests: `Pll` runs the clock
  on synthetic bursty arrivals (5 frames per 185 ms burst, a 2 s stall, a rate
  change) and asserts monotonicity, spacing and long-term tracking.
- Tests: `MediaChain` runs the exact production chain against synthetic 30 fps
  H.264 + Opus RTP sources into a file via fdsink and asserts 1/30 s PTS
  spacing and A/V alignment on the Matroska output and on ffmpeg's remux of it
  (this would have caught the 19 fps clock). Also caught during development:
  `-fflags +nobuffer` makes ffmpeg's Matroska demuxer drop a third of the
  frames, and `Gst.Iterator` is not Python-iterable in PyGObject 3.42.

### Added
- `binary_sensor.<camera>_bridge_stream` (connectivity, diagnostic): ON while the
  camera bridge is reachable and this camera's live session has video flowing.
  Attributes carry the bridge's per-session counters (`secs_since_frame`,
  `reconnect_attempts`, `reconnects_total`, `gave_up`, `nack_negotiated`,
  `audio_fallback`, `video_drops`, `audio_drops`) and its process-level leak
  indicators (`bridge_fds`, `bridge_ffmpeg_children`, `bridge_wedged_sessions`,
  `bridge_version`, ...). A bridge that is down shows OFF with a `reason`, not
  unavailable, so automations can trigger on it.
- `sensor.<camera>_bridge_reconnects` (total_increasing, diagnostic): how often
  the bridge had to restart this camera's session since it started.
- Both come from a new `BridgeHealthCoordinator` polling `GET {bridge_url}/health`
  every 30 s (5 s timeout). Needs gst_bridge >= the 2026-11-12 build.

### Changed
- Config flow and options flow probe `{bridge_url}/health` and refuse an
  unreachable bridge URL with a clear message (the #1 support issue was a URL
  pointing at a loopback-only or wrong host).
- Camera: logs name the device and bridge URL; a `/start` that returns
  `started: false` is logged as a warning instead of silently handing HA a path
  nobody publishes yet.
- gst_bridge compose: both containers run as uid 65534 with a read-only root
  filesystem, all capabilities dropped and `no-new-privileges`; `/tmp` is a
  tmpfs (GStreamer registry, Python caches). Healthcheck `start_period` 60 s.
  Dockerfile pinned to `debian:bookworm-slim` by digest; `gstreamer1.0-libav`
  dropped (not in the data path; the image stays ~1.1 GB because ffmpeg itself
  needs the libav libraries).

### Removed
- The one-time diagnostic dumps (`telus_smarthome_plus_dump.json`,
  `_livestream.json`, `_clips.json`). The livestream dump wrote a temporary AWS
  credential set and the channel ARN world-readable into `/config`, where it
  also rode along in backups. Delete those three files if they exist.

### Fixed
- `download_clip` / the save folder option: a folder is only accepted under one
  of Home Assistant's media directories; the pruner deletes only files this
  integration wrote (`<stamp>_<event>_<clipid>.mp4`), never every `.mp4` under
  the folder; a device id of `..` can no longer escape the save folder.
- Clip remux no longer whitelists the `file` protocol for ffmpeg, so a hostile
  playlist cannot reference local files.
- API errors log at most 200 characters of a response body and never a full
  token response.
- `camera.py` docstring pointed at a `kvs_bridge/` directory that does not exist.

### Deployment note (this household, 2026-11-12)
- MediaMTX (`MTX_RTSPADDRESS: 127.0.0.1:8556`) and the bridge API
  (`BRIDGE_HOST: 127.0.0.1`) are bound to loopback in the local compose
  override; HA's `go2rtc.yaml` doorbell sources and the integration's bridge URL
  point at `127.0.0.1`. Before this any device on the IoT VLAN could read the
  doorbell stream or publish its own video onto the path.

### Fixed (gst_bridge)
- **Review follow-up (same day): defects found by four independent code reviews.**
  Full list with evidence in `docs/BACKLOG.md`; applied here:
  - The stale clock started at the Unix epoch: `started` was set at pad-added
    while `last_sample_ts` was still 0, so a freshly linked session was judged
    stale on the watchdog's next 3 s tick (`stale (no frames 1794449565.4s)` in
    the logs) and burned a reconnect attempt before its first keyframe. The
    clock now starts at pad-added.
  - A `/start` that reused a healthy or connecting session discarded the fresh
    credentials HA had just fetched; auto-reconnects then ran on the original,
    expiring ones (`ExpiredTokenException`). The reuse path now swaps in the
    new live config and resets the attempt counter.
  - `_write_pipe` used `select()`, which raises for any fd >= 1024 (reachable
    since nofile was raised to 65536) and would have truncated every keyframe
    silently while P-frames kept the heartbeat alive; now `poll()`. Pipes are
    grown to 1 MiB (`PIPE_BUFFER_BYTES`) so a 115 KB keyframe is one write,
    and the video write bound is 0.5 s (`PIPE_WRITE_TIMEOUT_SEC`) so a stalled
    ffmpeg cannot back-pressure the jitterbuffer for long.
  - After any dropped or partial video write the stream is corrupt until the
    next keyframe; delta frames are now skipped until an IDR arrives instead
    of being forwarded as smear.
  - Pipe writes and `_kill_ffmpeg`'s pipe closes are serialised per pipe so a
    write in flight can never land on a reused fd number.
  - The watchdog never healed a session that never produced a frame (signaling
    or ffmpeg failure) or whose thread had exited; both are now treated as
    stale. A GStreamer bus `message::error` ends the session's GLib loop.
    Giving up after `MAX_AUTO_RECONNECTS` is logged once at WARNING.
  - `stop()` racing `loop.run()` could lose the quit; the quit is also
    scheduled as an idle source. `_teardown` closes the signaling websocket,
    removes the bus watch and drops the webrtcbin reference so libnice sockets
    are freed immediately (fd count after a reconnect now equals a fresh start).
  - `STOP_TIMEOUT_SEC` is never below the kill path's worst case (grace + 3 s).
  - `/start` validates `device_id` (`[A-Za-z0-9_.:-]{1,64}`) and the live
    config BEFORE stopping the running session (a malformed body used to leave
    the camera with nothing to heal and then 500); caps concurrent sessions
    (`MAX_SESSIONS`, 8) with 429; waits on an Event instead of polling.
    `/stop` takes the sessions lock. Popen failure in `_on_pad` closes its pipes.
    A second video pad or a pad after `stop()` no longer spawns an untracked ffmpeg.
  - Logs: timestamps on every line, ffmpeg stderr relayed with the device id
    and pid, the 30 s `/health` access-log lines filtered out.
  - `/health`: `degraded` + `reasons`, `version`, `watchdog_age_s`, per-session
    `reconnects_total`, `gave_up`, `nack_negotiated`, `audio_fallback`;
    returns 503 only for process sickness a restart cures (watchdog thread
    silent, session wedged > 5 min), never for a camera outage.
  - `BRIDGE_HOST` (default unchanged, 0.0.0.0) to bind the control API to
    loopback; startup warning when `RTSP_READ_BASE` is loopback-only.
  - Image stamped with the git revision (`GIT_SHA` build arg → `BRIDGE_VERSION`
    in logs and `/health`).
  - Tests: `gst_bridge/tests/test_bridge.py` (stdlib unittest, runs inside the
    image) covers pipe-write bounds incl. fd >= 1024, ffmpeg kill paths, the
    audio writer on a full pipe, the reaper, IDR-wait after a drop, the exact
    production video chain against a synthetic 30 fps RTP source, live-config
    parsing/signing and device-id validation.
  - Tried and reverted: `-use_wallclock_as_timestamps` for the video input.
    The fixed-19-fps timestamp problem is documented in `docs/BACKLOG.md` B1.
- **Frigate recordings of the doorbell were full of artifacting (smeared, blocky
  frames and multi-second freezes).** Two causes, both in the bridge:
  1. *Leaked sessions.* Every stale-session restart left the previous ffmpeg
     child alive: `stop()` only sent SIGTERM and never closed the pipes, and
     ffmpeg 5.1 blocked in `read()` on the video pipe does not act on that flag.
     Each leaked ffmpeg pinned an audio-writer thread in `write()` on its full
     audio pipe, and its webrtcbin kept its libnice sockets. After 9 h the bridge
     had 18 ffmpeg children, 92 threads and 803 of its 1024 file descriptors
     (gupnp already logging "Too many open files"); the kernel counted 5.8 M
     UDP receive-buffer drops on the dead sockets. `_kill_ffmpeg()` now closes
     both pipes (EOF makes ffmpeg finish), then SIGTERM, then SIGKILL after
     `FFMPEG_KILL_GRACE_SEC` (3 s); pipe writes go through `_write_pipe()` with a
     bound (`PIPE_WRITE_TIMEOUT_SEC`, 2 s video / 0.5 s audio) and drop instead
     of blocking a GStreamer streaming thread or the audio writer for good; the
     watchdog reaps any stopped session whose ffmpeg is still alive; the compose
     file raises `nofile` to 65536; `/health` reports `process.fds`, `threads`,
     `ffmpeg_children`, `session_objects`, `wedged_sessions` and per-session
     `video_drops` / `audio_drops`.
  2. *No loss recovery on the WebRTC leg.* KVS delivers RTP over UDP; a
     1440x1920 keyframe is ~115 KB = ~95 packets, so one lost packet discarded
     the whole keyframe and every P-frame of that GOP then referenced a picture
     that never arrived. Measured before the fix: 6 of 50 keyframes lost in
     45 s on a quiet night, 12 of 14 inside one 17 s recording (36 decodable
     frames out of 250, the rest "no picture ooo"). The video transceiver now
     sets `do-nack` (the camera answers `a=rtcp-fb:101 nack` and `nack pli`),
     webrtcbin `latency` is `WEBRTC_JITTER_MS` (300 ms) so a retransmit has time
     to land, and the depayloader runs `request-keyframe=true` (PLI on loss)
     `wait-for-keyframe=true` with AU alignment so a broken GOP is dropped
     rather than forwarded smeared. Measured after: 44 of 44 keyframes in 45 s,
     0 decoder-dropped frames. `WEBRTC_NACK=0` turns the NACK negotiation off.
- The bridge could hang for good after a stale-session restart. `GstSession.stop()`
  tore the GStreamer pipeline down (`set_state(NULL)`) in the caller's thread; when
  webrtcbin was wedged that call never returned, and because HA's next
  `/session/<id>/start` ran it inside the aiohttp event loop, `/health` and
  `/start` stopped answering entirely (observed 2026-11-04 22:23 UTC: 27 h
  unhealthy, HA logging "Bridge unreachable" every minute, the doorbell stream
  dark). Teardown now runs on the session's own thread after its GLib loop
  exits, `stop()` only issues quick bounded calls (websocket close with a 1 s
  timeout, ffmpeg terminate, loop quit), callers wait at most `STOP_TIMEOUT_SEC`
  (5) for it, and the start handler awaits instead of sleeping on the loop.
  `SIGUSR1` now dumps every thread's stack (`faulthandler`) so a future hang
  can be located from `docker logs`.

## [0.12.1]

### Fixed
- `camera_proxy` for a camera whose API device carries no `thumbnailUrl` answered
  500 on every dashboard load (card poster, more-info preview, notification
  images). `async_camera_image` now falls back to one frame from Home Assistant's
  bundled go2rtc (`/api/frame.jpeg?src=<entity_id>` on loopback), which already
  relays the camera's stream, and returns None only when that fails too.
- The raw device-list dump (`telus_smarthome_plus_dump.json`) is written once,
  like the live-stream dump, instead of being rewritten with a WARNING on every
  start.

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
- Service `telus_smarthome_plus.download_clip` (optional `device_id`, `clip_id`;
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
- The first clip-list response is written to `/config/telus_smarthome_plus_clips.json`
  to help confirm field names across firmware/regions.
- `tools/fetch_clips.py` — standalone script to list/download clips to MP4.

## [0.4.1] — 2026-10-22

### Changed
- **Brand images now ship with the integration** at
  `custom_components/telus_smarthome_plus/brand/` (icon + logo). Home Assistant
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

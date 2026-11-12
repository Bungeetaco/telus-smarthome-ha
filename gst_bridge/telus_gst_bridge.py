#!/usr/bin/env python3
"""TELUS KVS-WebRTC -> RTSP bridge using GStreamer webrtcbin.

aiortc could complete the KVS handshake but never received media. GStreamer's
webrtcbin (libnice ICE + libwebrtc-grade media) handles it properly. The HA
integration POSTs the per-view live config (channel ARN + region + temp AWS
creds + signaling endpoints); this service does the KVS viewer handshake with
webrtcbin and republishes the received H.264 unchanged (plus the camera's Opus
audio transcoded to AAC) through an ffmpeg child to MediaMTX over RTSP
(rtph264depay -> h264parse -> appsink -> ffmpeg -c:v copy -f rtsp).

HTTP control API (unchanged from the aiortc version):
  POST /session/{device_id}/start   body {deviceId, liveConfig}
  POST /session/{device_id}/stop
  GET  /health
"""
from __future__ import annotations

import asyncio
import base64
import faulthandler
import fcntl
import hmac
import json
import os
import re
import signal
import subprocess
import threading
import time
import uuid
from urllib.parse import quote, urlencode

import boto3
from botocore.auth import SigV4QueryAuth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials
from aiohttp import web
import websocket  # websocket-client (sync)

import gi
gi.require_version("Gst", "1.0")
gi.require_version("GstVideo", "1.0")
gi.require_version("GstWebRTC", "1.0")
gi.require_version("GstSdp", "1.0")
from gi.repository import Gst, GstVideo, GstWebRTC, GstSdp, GLib  # noqa: E402

Gst.init(None)

import logging
# Timestamps in every line: ffmpeg's stderr and GStreamer warnings are relayed
# through this logger too, so a Frigate recording can be lined up with the log
# without `docker logs -t`.
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
_LOG = logging.getLogger("telus-gst-bridge")


def _env_flag(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default) not in ("0", "false", "no", "")


RTSP_PUBLISH_BASE = os.environ.get("RTSP_PUBLISH_BASE", "rtsp://mediamtx:8554")
RTSP_READ_BASE = os.environ.get("RTSP_READ_BASE", "rtsp://mediamtx:8554")
LISTEN_PORT = int(os.environ.get("BRIDGE_PORT", "8787"))
# Bind address of the control API. HA, go2rtc and Frigate usually run on the
# same host; set BRIDGE_HOST=127.0.0.1 then, so nothing else on the LAN can
# stop a session or point the bridge at its own signaling server.
BRIDGE_HOST = os.environ.get("BRIDGE_HOST", "0.0.0.0")
# Shared secret for /session/*: when set, callers must send it in the
# X-Bridge-Token header (Home Assistant: the integration's "bridge token"
# option). /health stays open: it carries no secrets and the Docker
# healthcheck reads it. Empty = no authentication (loopback deployments).
BRIDGE_TOKEN = os.environ.get("BRIDGE_TOKEN", "")
# Only AWS Kinesis Video signaling endpoints may be dialled: a caller who can
# reach the API must not be able to point the bridge (the one VM the firewall
# lets through) at an arbitrary host as a reachability oracle.
ENDPOINT_ALLOW_RE = re.compile(os.environ.get(
    "ENDPOINT_ALLOW_RE",
    r"^(wss|https)://[a-z0-9.-]+\.kinesisvideo\.[a-z0-9-]+\.amazonaws\.com(/.*)?$"))
BRIDGE_VERSION = os.environ.get("BRIDGE_VERSION", "unknown")
# device_id ends up in the RTSP path, an ffmpeg argument and every log line.
DEVICE_ID_RE = re.compile(r"[A-Za-z0-9_.:-]{1,64}")
# Sessions are keyed by caller-chosen device_id; without a cap an unauthenticated
# caller could grow threads/ffmpeg children until pids_limit starves the real one.
MAX_SESSIONS = int(os.environ.get("MAX_SESSIONS", "8"))

# Self-healing tuning. The camera caps a live session (~10 min) and then drops
# it; the bridge must notice and restart instead of serving a dead session.
STREAM_STALE_SEC = float(os.environ.get("STREAM_STALE_SEC", "8"))    # no frames => stale
STARTUP_GRACE_SEC = float(os.environ.get("STARTUP_GRACE_SEC", "25"))  # handshake window
WATCHDOG_INTERVAL_SEC = float(os.environ.get("WATCHDOG_INTERVAL_SEC", "3"))
MAX_AUTO_RECONNECTS = int(os.environ.get("MAX_AUTO_RECONNECTS", "6"))  # before waiting for HA
# Once the bridge has given up on a camera (MAX_AUTO_RECONNECTS failed, e.g. the
# camera is off Wi-Fi), a fresh attempt is made at most this often even though
# Home Assistant keeps asking every minute. Before 2026-11-12 the reuse path
# reset the attempt counter on every HA /start, so a dead doorbell was dialled
# every 27 s all morning (806 offers in 6 h); each attempt wakes the camera.
RESTART_BACKOFF_SEC = float(os.environ.get("RESTART_BACKOFF_SEC", "120"))
# ffmpeg child shutdown: its stdin pipes are closed first (EOF makes it finish
# on its own), then SIGTERM, then SIGKILL after this grace. Before 2026-11-12 the
# bridge only sent SIGTERM and never closed the pipes; ffmpeg 5.1 blocked in
# read() on the video pipe ignores that flag, so every stale-session restart
# left one ffmpeg (and one audio-writer thread blocked on its full pipe) behind:
# 17 leaked ffmpegs and 803 of 1024 fds after 9 h.
FFMPEG_KILL_GRACE_SEC = float(os.environ.get("FFMPEG_KILL_GRACE_SEC", "3"))
# How long a caller waits for a session's stop() before leaving it to finish in
# the background. Tearing down a wedged webrtcbin can block indefinitely. Never
# shorter than stop()'s own worst case (ws close 1 s + SIGTERM grace + SIGKILL
# wait 2 s), otherwise the watchdog spawns the replacement while the old ffmpeg
# still owns the MediaMTX path and the new publish is refused.
STOP_TIMEOUT_SEC = max(float(os.environ.get("STOP_TIMEOUT_SEC", "5")),
                       FFMPEG_KILL_GRACE_SEC + 3.0)
# Pipe capacity requested with F_SETPIPE_SZ for ffmpeg's stdin. The default
# 64 KiB is smaller than one 1440x1920 keyframe (~115 KB); 1 MiB lets fdsink
# hand a whole cluster over in one write.
PIPE_BUFFER_BYTES = int(os.environ.get("PIPE_BUFFER_BYTES", str(1 << 20)))
# webrtcbin adds its audio and video pads a few ms apart. The Matroska muxer
# wants every track before it starts, so the first pad starts this
# timer and the chain is built when the second pad arrives or the timer fires
# (video-only if audio never showed up).
PAD_SETTLE_MS = int(os.environ.get("PAD_SETTLE_MS", "1500"))
# Packet-loss handling on the WebRTC leg. KVS is UDP; a 1440x1920 keyframe is
# ~115 KB = ~95 RTP packets, so without retransmission a single lost packet
# destroys the whole keyframe. Measured 2026-11-12 before this: 6 of 50
# keyframes lost in 45 s on a quiet night, 12 of 14 in a busy 17 s recording,
# which is the "artifacting" in Frigate's recordings (P-frames referencing a
# keyframe that never arrived). do-nack negotiates RTCP NACK so lost packets are
# re-sent; the jitterbuffer has to hold packets long enough for the retransmit
# to arrive (one RTT to the camera via AWS). request-keyframe sends a PLI when
# loss is detected anyway, and wait-for-keyframe drops the broken remainder of
# the GOP instead of forwarding smeared frames.
WEBRTC_NACK = _env_flag("WEBRTC_NACK")
WEBRTC_JITTER_MS = int(os.environ.get("WEBRTC_JITTER_MS", "300"))

# Audio (Opus -> AAC). The camera's Opus is muxed, still encoded, into the same
# Matroska stream as the video (timestamps from the same jitterbuffer, so A/V
# stay in sync); ffmpeg decodes it, boosts it and encodes AAC for RTSP. If a
# session fails to come up with audio, the bridge retries WITHOUT audio, so
# audio can never take video down. Set ENABLE_AUDIO=0 to turn it off entirely.
ENABLE_AUDIO = _env_flag("ENABLE_AUDIO")
AUDIO_FALLBACK_AFTER = int(os.environ.get("AUDIO_FALLBACK_AFTER", "2"))  # failed reconnects before dropping audio
# The camera's mic is quiet; boost it (dB) with a limiter so loud bangs don't clip.
AUDIO_GAIN_DB = float(os.environ.get("AUDIO_GAIN_DB", "15"))


# --------------------------------------------------------------------------- #
# live-config parsing (same confirmed shape as before)
# --------------------------------------------------------------------------- #
def parse_live_config(payload: dict) -> dict:
    cfg = payload.get("liveConfig") or payload
    for key in ("data", "body", "result"):
        if isinstance(cfg.get(key), dict) and "channelArn" in cfg[key]:
            cfg = cfg[key]
    client = cfg.get("client") or {}
    creds = client.get("credentials") or {}
    eps = {
        e.get("Protocol"): e.get("ResourceEndpoint")
        for e in client.get("resourceEndpointList", [])
        if isinstance(e, dict)
    }
    return {
        "channel_arn": cfg["channelArn"],
        "region": cfg.get("region") or "ca-central-1",
        "access_key": creds.get("AccessKeyId"),
        "secret_key": creds.get("SecretAccessKey"),
        "session_token": creds.get("SessionToken"),
        "wss_endpoint": eps.get("WSS"),
        "https_endpoint": eps.get("HTTPS"),
        "session_id": cfg.get("sessionId") or str(uuid.uuid4()),
    }


def endpoint_allowed(url) -> bool:
    """True for a signaling endpoint the bridge may dial (see ENDPOINT_ALLOW_RE)."""
    return isinstance(url, str) and ENDPOINT_ALLOW_RE.match(url) is not None


def check_endpoints(cfg: dict) -> str | None:
    """Return an error string if a live config points outside AWS KVS."""
    for key in ("wss_endpoint", "https_endpoint"):
        if cfg.get(key) and not endpoint_allowed(cfg[key]):
            return f"{key} not allowed: {cfg[key][:80]}"
    if not cfg.get("wss_endpoint"):
        return "wss_endpoint missing"
    return None


def authorized(request) -> bool:
    """Shared-secret check for the session endpoints (no-op without a token)."""
    if not BRIDGE_TOKEN:
        return True
    given = request.headers.get("X-Bridge-Token", "")
    return hmac.compare_digest(given.encode(), BRIDGE_TOKEN.encode())


def sign_wss_url(c: dict, client_id: str) -> str:
    params = {"X-Amz-ChannelARN": c["channel_arn"], "X-Amz-ClientId": client_id}
    url = f"{c['wss_endpoint']}?{urlencode(params)}"
    req = AWSRequest(method="GET", url=url)
    creds = Credentials(c["access_key"], c["secret_key"], c["session_token"])
    SigV4QueryAuth(creds, "kinesisvideo", c["region"], expires=299).add_auth(req)
    return req.url


def get_ice_servers(c: dict, client_id: str) -> list[str]:
    """Return webrtcbin-style STUN/TURN URIs."""
    uris = [f"stun://stun.kinesisvideo.{c['region']}.amazonaws.com:443"]
    try:
        sig = boto3.client(
            "kinesis-video-signaling",
            region_name=c["region"],
            endpoint_url=c["https_endpoint"],
            aws_access_key_id=c["access_key"],
            aws_secret_access_key=c["secret_key"],
            aws_session_token=c["session_token"],
        )
        resp = sig.get_ice_server_config(ChannelARN=c["channel_arn"], ClientId=client_id)
        for ice in resp.get("IceServerList", []):
            user = quote(ice.get("Username", ""), safe="")
            pw = quote(ice.get("Password", ""), safe="")
            for u in ice.get("Uris", []):
                # u like "turn:host:port?transport=udp" -> turn://user:pw@host:port?...
                scheme, rest = u.split(":", 1)
                uris.append(f"{scheme}://{user}:{pw}@{rest}")
    except Exception as err:  # noqa: BLE001
        _LOG.warning("GetIceServerConfig failed (STUN only): %s", err)
    return uris


# --------------------------------------------------------------------------- #
# One GStreamer webrtcbin viewer session (runs in its own thread + GLib loop)
# --------------------------------------------------------------------------- #
# Every session ever created, so the watchdog can reap what a bounded stop()
# left behind (a wedged pipeline teardown must not keep an ffmpeg alive).
_ALL_SESSIONS: list["GstSession"] = []
_ALL_SESSIONS_LOCK = threading.Lock()

# The production media chain, in one place so a test can run the exact same
# description against videotestsrc/x264enc/rtph264pay (+ audiotestsrc/opusenc).
#
# Why a container at all: a raw Annex-B pipe carries no timestamps, so ffmpeg used to
# stamp every frame from the SPS VUI (a fixed 19 fps on this camera) while the
# real rate is 14-30 fps -- recordings ran fast or slow against their audio and
# ffmpeg's interleaver held one stream back for seconds. The muxer writes the
# jitterbuffer's PTS for both tracks into the container; ffmpeg reads them.
#
# request-keyframe: a PLI goes back to the camera when the depayloader sees RTP
# loss; wait-for-keyframe: the rest of that GOP is dropped rather than forwarded
# smeared (needs AU alignment, hence the capsfilter right after the depayloader).
# Timestamps: this camera's RTP clock is frame-count based at its nominal
# 19 fps (like its SPS VUI) while it really captures 14-30 fps, so RTP time
# runs up to 1.4x real time. rtpjitterbuffer (slave mode) paces output to that
# clock and re-syncs every few seconds (forward bursts, backward jumps,
# "Non-monotonous DTS" in ffmpeg). The only honest timebase is packet ARRIVAL:
# a probe on each jitterbuffer's sink pad records the running time per RTP
# sequence number and a probe on its src pad writes it back as the packet PTS,
# so the depayloaders stamp every access unit with when its first packet
# arrived (retransmitted packets excepted; a per-track monotonic clamp covers
# that). Delivery is still bursty (the jitterbuffer's pacing), the timestamps
# are not.
#
# The per-branch leaky queues isolate the camera from a stalled ffmpeg: fdsink
# blocks when the pipe is full, the queues then drop upstream instead of
# back-pressuring the jitterbuffer, and the heartbeat (a probe on fdsink's
# sink pad) stops, so the watchdog restarts the session.
# Container: MPEG-TS, not Matroska. Matroska (tried first, 2026-11-12) fails
# with "Could not multiplex stream" the moment the camera changes its H.264
# parameters mid-session (a new SPS on a day/night or bitrate switch): a
# streamable Matroska header cannot take new codec data, and that ended a
# 95-minute session and tripped Frigate. MPEG-TS carries PTS just the same but
# takes H.264 as byte-stream with SPS/PPS in-band, so parameter changes simply
# flow through, exactly as the raw pipe used to tolerate.
# The depayloader is asked for AVC (codec_data from the SDP's
# sprop-parameter-sets, or from the first in-band SPS/PPS) and h264parse
# converts to byte-stream with config-interval=-1, so SPS/PPS sit in front of
# EVERY keyframe including the first one. Without that the stream opened on an
# IDR the camera sent without its parameter sets, ffmpeg's probe ended with
# "Could not find codec parameters ... unspecified size" and the session died.
VIDEO_BRANCH = (
    "rtph264depay name=vdepay request-keyframe=true wait-for-keyframe=true ! "
    "video/x-h264,stream-format=avc,alignment=au ! h264parse config-interval=-1 ! "
    "video/x-h264,stream-format=byte-stream,alignment=au ! "
    "queue name=vq leaky=downstream max-size-time=5000000000 max-size-buffers=0 max-size-bytes=0 ! "
    "mux.sink_64"   # mpegtsmux pads are named by PID; PIDs below 0x40 are reserved
)
# Audio is CONTINUOUS by construction. mpegtsmux (a GstAggregator without a
# next-time hook) emits nothing until every pad has a buffer, so a camera that
# pauses its audio (Opus DTX, Wi-Fi hiccup) used to stall the video with it and
# past 8 s the watchdog restarted a healthy session (BACKLOG B3). And a session
# without audio (no pad within PAD_SETTLE_MS, or the video-only fallback)
# changed MediaMTX's track count under running readers, so go2rtc/Frigate had
# to reconnect and recordings lost sound (BACKLOG C4). Now the camera's Opus is
# decoded into a live audiomixer that also has a silence source, and the mix
# is re-encoded: there is always exactly one audio track, and a pause on the
# camera side is silence, not a stall. The real audio arrives up to
# AUDIO_MIX_LATENCY_MS after its (smoothed) timestamp; the mixer waits that
# long before it fills the slot with silence.
AUDIO_MIX_LATENCY_MS = int(os.environ.get("AUDIO_MIX_LATENCY_MS", "1000"))
AUDIO_RAW_CAPS = "audio/x-raw,rate=48000,channels=1,format=S16LE,layout=interleaved"
AUDIO_BRANCH = (
    "rtpopusdepay name=adepay ! opusparse ! opusdec plc=true ! audioconvert ! audioresample ! "
    + AUDIO_RAW_CAPS + " ! level name=clevel interval=2000000000 ! "
    "queue name=aqin leaky=downstream max-size-time=3000000000 max-size-buffers=0 max-size-bytes=0 ! "
    "amix."
)
AUDIO_MIX = (
    "audiotestsrc name=silence wave=silence is-live=true samplesperbuffer=960 ! "
    + AUDIO_RAW_CAPS + " ! queue ! amix. "
    "audiomixer name=amix latency=%d ! " % (AUDIO_MIX_LATENCY_MS * 1_000_000)
    + AUDIO_RAW_CAPS + " ! level name=mlevel interval=2000000000 ! "
    "opusenc name=aenc bitrate=64000 ! "
    "queue name=aq leaky=downstream max-size-time=5000000000 max-size-buffers=0 max-size-bytes=0 ! "
    "mux.sink_65"
)
MUX_CHAIN = "mpegtsmux name=mux ! fdsink name=out sync=false fd=%d"
# The heartbeat probe on fdsink must accept buffer LISTS: mpegtsmux pushes one
# list per PES and never a single buffer, so a BUFFER-only probe never fires
# and the watchdog kills a healthy session as "stale" after 8 s (2026-11-12).
OUT_PROBE_TYPE = Gst.PadProbeType.BUFFER | Gst.PadProbeType.BUFFER_LIST


def build_chain(fd: int, audio: bool) -> str:
    """gst-launch description of everything downstream of webrtcbin's pads."""
    desc = MUX_CHAIN % fd + " " + VIDEO_BRANCH + " " + AUDIO_MIX
    if audio:
        desc += " " + AUDIO_BRANCH
    return desc


class _Pll:
    """Smooth, monotonic presentation clock from bursty arrival times.

    The doorbell captures at a steady rate (14-30 fps) but transmits over Wi-Fi
    in bursts: several frames within 2 ms, then 200-500 ms of nothing, and
    after a stall a whole second or two of frames at once (measured on the wire
    2026-11-12). Its RTP timestamps are frame-count based at a fixed nominal
    rate, so they cannot be used either. This tracker outputs
    pts_n = pts_{n-1} + T, where T is the mean arrival interval over the last
    `window_ns` of arrivals (time-based, so a burst cannot collapse it), pulls
    the clock toward the arrival time with gain `k` so it never drifts away
    from real time, floors each step at a third of the nominal interval (no
    fast-forward after a burst) and caps it at 4 T (catch-up after a stall is
    gradual, not a jump).
    """

    def __init__(self, default_interval_ns: int, window_ns: int = 3_000_000_000,
                 k: float = 0.05, max_lead_ns: int = 1_000_000_000):
        self.default = default_interval_ns
        self.window_ns = window_ns
        self.k = k
        self.max_lead = max_lead_ns
        self.arrivals: list[int] = []
        self.pts: int | None = None

    def update(self, arrival_ns: int) -> int:
        self.arrivals.append(arrival_ns)
        cutoff = arrival_ns - self.window_ns
        while len(self.arrivals) > 2 and self.arrivals[0] < cutoff:
            del self.arrivals[0]
        if len(self.arrivals) > 600:            # memory bound at silly rates
            del self.arrivals[: len(self.arrivals) - 600]
        if self.pts is None:
            self.pts = arrival_ns
            return self.pts
        span = self.arrivals[-1] - self.arrivals[0]
        if len(self.arrivals) >= 10 and span > 0:
            interval = span // (len(self.arrivals) - 1)
        else:
            interval = self.default
        step = interval + int(self.k * (arrival_ns - (self.pts + interval)))
        floor = max(self.default // 3, 1_000_000)
        step = max(floor, min(step, 4 * interval))
        pts = self.pts + step
        if pts > arrival_ns + self.max_lead:     # never run far ahead of reality
            pts = max(self.pts + floor, arrival_ns + self.max_lead)
        self.pts = pts
        return pts


class _Hist:
    """Inter-arrival histogram (ms buckets) + max gap, for /health diagnostics."""
    BUCKETS = ((2, "<2ms"), (20, "2-20ms"), (100, "20-100ms"), (500, "100-500ms"), (10**9, ">500ms"))

    def __init__(self):
        self.last = None
        self.counts = {name: 0 for _, name in self.BUCKETS}
        self.max_gap_ms = 0.0

    def tick(self, now_ns: int) -> None:
        if self.last is not None:
            gap_ms = (now_ns - self.last) / 1e6
            if gap_ms > self.max_gap_ms:
                self.max_gap_ms = gap_ms
            for limit, name in self.BUCKETS:
                if gap_ms < limit:
                    self.counts[name] += 1
                    break
        self.last = now_ns

    def as_dict(self) -> dict:
        return {**self.counts, "max_gap_ms": round(self.max_gap_ms, 1)}


def _grow_pipe(fd: int) -> None:
    """Best effort F_SETPIPE_SZ so one keyframe fits in a single write
    (unprivileged up to /proc/sys/fs/pipe-max-size, 1 MiB by default)."""
    try:
        fcntl.fcntl(fd, fcntl.F_SETPIPE_SZ, PIPE_BUFFER_BYTES)
    except (OSError, AttributeError) as err:
        _LOG.warning("could not grow pipe buffer to %d: %s", PIPE_BUFFER_BYTES, err)


class GstSession:
    def __init__(self, device_id: str, cfg: dict, publish_url: str, read_url: str,
                 audio: bool = False):
        self._stop_ts = 0.0                # when stop() was first called (0 = running)
        self._ffmpeg_lock = threading.Lock()
        self._rtcp_fb_logged = False
        self.nack_negotiated = None        # True/False once the SDP answer is seen
        self.ffmpeg_exited = False
        self.video_drops = 0               # video AUs dropped because ffmpeg stopped reading
        self.audio_drops = 0
        self._gave_up_logged = False
        self.reconnects_total = 0          # monotonic, carried across respawns (a counter, unlike reconnect_attempts)
        self.started_evt = threading.Event()
        self._ws_lock = threading.Lock()
        self.audio = audio                 # request + publish audio for this session
        self.audio_active = False          # camera audio has reached the muxer
        self.audio_bytes = 0
        self.video_bytes = 0
        self._pads: dict[str, Gst.Pad] = {}  # "video"/"audio" pads from webrtcbin
        self._settle_id = 0                # GLib timeout waiting for the 2nd pad
        self._chain_built = False
        self._chain = None
        # webrtcbin emits pad-added for audio and video from two streaming
        # threads at the same time; building the chain and linking must not
        # interleave (the second handler once saw chain_built with no chain).
        self._pad_lock = threading.Lock()
        self._arrival: dict[int, dict[int, int]] = {}   # ssrc -> {seq: arrival running time (ns)}
        self._pll = {"video": _Pll(33_000_000), "audio": _Pll(20_000_000)}
        self._au_no_pts = 0
        self._kf_seen = False          # video: first keyframe passed the gate
        self._pre_kf_dropped = 0       # video AUs dropped before it
        self._pre_kf_audio_dropped = 0 # audio AUs dropped before it
        self._last_kf_request = 0.0
        self._last_au_ts = {"video": 0.0, "audio": 0.0}
        self.audio_rms_db: dict[str, float] = {}   # from the `level` meters
        self._restamped = 0
        self._unstamped = 0                              # packets whose arrival was unknown
        # Diagnostics: inter-arrival histograms at two points (see /health).
        self._hist = {"nicesrc": _Hist(), "jb_in": _Hist()}
        self._nicesrc_probed = False
        self.device_id = device_id
        self.cfg = cfg
        self.publish_url = publish_url
        self.read_url = read_url
        self.client_id = "ha-" + uuid.uuid4().hex[:12]
        self.pipe = None
        self.webrtc = None
        self.ws = None
        self.loop = None
        self.ffmpeg = None
        self.started = False
        self.created_ts = time.time()      # when this session was created
        self.last_sample_ts = 0.0          # last time a video frame was seen
        self.reconnect_attempts = 0        # auto-reconnects since last fresh /start
        self._ws_ready = False
        self._ws_queue = []
        self._stopping = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        with _ALL_SESSIONS_LOCK:
            _ALL_SESSIONS.append(self)

    def is_healthy(self) -> bool:
        """True while video is actually flowing (a frame within STREAM_STALE_SEC)."""
        return (
            self.started
            and (time.time() - self.last_sample_ts) < STREAM_STALE_SEC
        )

    def is_connecting(self) -> bool:
        """True during the initial handshake grace, before frames arrive."""
        return (
            not self.started
            and (time.time() - self.created_ts) < STARTUP_GRACE_SEC
        )

    def start(self):
        self._thread.start()

    def stop(self):
        """Ask the session to shut down. Only quick, bounded calls happen here;
        the GStreamer teardown runs on the session's own thread (see _run), so a
        wedged webrtcbin can never hang the caller.

        Why: 2026-11-04 22:23 UTC the watchdog restarted a stale doorbell
        session and the bridge's HTTP loop never answered again (27 h of
        /health timeouts, HA logging "Bridge unreachable" every minute). The
        old stop() ran pipe.set_state(NULL) in the caller's thread, and HA's
        next /start called it from inside the aiohttp event loop.
        """
        if not self._stop_ts:
            self._stop_ts = time.time()
        self._stopping.set()
        try:
            if self.ws:
                self.ws.close(timeout=1)
        except Exception:
            pass
        loop = self.loop
        if loop:
            # quit() on a loop that is not running yet is lost (the run() that
            # follows sets is_running and never returns); the idle source runs
            # as soon as the loop actually runs, so both cases are covered.
            loop.quit()
            GLib.idle_add(lambda: (loop.quit(), False)[1])
        # Bounded (pipes closed -> EOF, SIGTERM, SIGKILL after the grace); this
        # runs on the stop() helper thread, never on the aiohttp loop.
        self._kill_ffmpeg()

    def _kill_ffmpeg(self) -> None:
        """Close ffmpeg's stdin and make sure the child is gone. Idempotent,
        safe from any thread, bounded to FFMPEG_KILL_GRACE_SEC + 2 s.

        Closing the pipe matters more than the signal: ffmpeg 5.1 blocked in
        read() does not act on SIGTERM, but EOF makes it finish. fdsink holds
        the same fd number; after the close it errors out (EBADF/EPIPE), which
        ends the GLib loop via the bus -- fine, the session is stopping."""
        with self._ffmpeg_lock:
            proc, self.ffmpeg = self.ffmpeg, None
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except Exception:
            pass
        if proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass
            try:
                proc.wait(timeout=FFMPEG_KILL_GRACE_SEC)
            except subprocess.TimeoutExpired:
                _LOG.warning("[%s] ffmpeg %d ignored SIGTERM for %.0fs; killing",
                             self.device_id, proc.pid, FFMPEG_KILL_GRACE_SEC)
                try:
                    proc.kill()
                    proc.wait(timeout=2)
                except Exception:
                    pass
            except Exception:
                pass
        self.ffmpeg_exited = True

    def ffmpeg_alive(self) -> bool:
        proc = self.ffmpeg
        return proc is not None and proc.poll() is None

    def _teardown(self):
        """Release the pipeline. Runs on the session thread only."""
        self._kill_ffmpeg()  # idempotent; covers a stop() that never ran
        try:
            if self.ws:  # stop() may have run before the websocket existed
                self.ws.close(timeout=1)
        except Exception:
            pass
        pipe, self.pipe = self.pipe, None
        if pipe is None:
            return
        try:
            pipe.set_state(Gst.State.NULL)
        except Exception as err:  # noqa: BLE001
            _LOG.warning("[%s] pipeline teardown failed: %s", self.device_id, err)
        try:
            pipe.get_bus().remove_signal_watch()
        except Exception:
            pass
        # Break the reference cycles session -> webrtcbin/chain -> signal and
        # probe closures -> session, so the pipeline (libnice's UDP sockets,
        # fdsink's fd) is finalised now rather than whenever the cyclic GC
        # gets to it.
        self.webrtc = None
        self._chain = None
        self._pads = {}
        _LOG.info("[%s] session torn down (video drops=%d, audio drops=%d)",
                  self.device_id, self.video_drops, self.audio_drops)

    # ---- thread entry ----
    def _run(self):
        try:
            self._run_inner()
        finally:
            self._teardown()

    def _run_inner(self):
        dev = self.device_id
        try:
            ice = get_ice_servers(self.cfg, self.client_id)
            stun = next((u for u in ice if u.startswith("stun")), None)
            turns = [u for u in ice if u.startswith("turn")]

            self.pipe = Gst.Pipeline.new(f"pipe-{dev}")
            self.webrtc = Gst.ElementFactory.make("webrtcbin", "wb")
            self.webrtc.set_property("bundle-policy", "max-bundle")
            # Jitterbuffer depth: must cover one retransmission round trip for
            # NACK to be worth anything (default 200 ms).
            self.webrtc.set_property("latency", WEBRTC_JITTER_MS)
            if stun:
                self.webrtc.set_property("stun-server", stun)
            self.pipe.add(self.webrtc)
            for t in turns:
                self.webrtc.emit("add-turn-server", t)
            rtpbin = self.webrtc.get_by_name("rtpbin")
            if rtpbin is not None:
                rtpbin.connect("new-jitterbuffer", self._on_new_jitterbuffer)
            else:
                _LOG.warning("[%s] webrtcbin has no 'rtpbin' child; arrival-time "
                             "stamping disabled (timestamps follow the camera clock)", dev)

            # recvonly video+audio transceivers
            caps_v = Gst.Caps.from_string(
                "application/x-rtp,media=video,encoding-name=H264,payload=101,clock-rate=90000"
            )
            trans_v = self.webrtc.emit(
                "add-transceiver",
                GstWebRTC.WebRTCRTPTransceiverDirection.RECVONLY, caps_v,
            )
            if WEBRTC_NACK and trans_v is not None:
                # Puts "a=rtcp-fb:101 nack" in the offer so lost RTP packets are
                # retransmitted instead of taking the whole keyframe with them.
                trans_v.set_property("do-nack", True)

            if self.audio:
                caps_a = Gst.Caps.from_string(
                    "application/x-rtp,media=audio,encoding-name=OPUS,payload=111,"
                    "clock-rate=48000,encoding-params=(string)2"
                )
                self.webrtc.emit(
                    "add-transceiver",
                    GstWebRTC.WebRTCRTPTransceiverDirection.RECVONLY, caps_a,
                )

            self.webrtc.connect("on-negotiation-needed", self._on_neg)
            self.webrtc.connect("on-ice-candidate", self._on_local_ice)
            self.webrtc.connect("pad-added", self._on_pad)

            # A pipeline ERROR (DTLS failure, depayloader error, MediaMTX gone
            # mid-stream...) used to leave the GLib loop running forever with a
            # session that never produced a frame; end the loop so the watchdog
            # sees a dead thread and respawns.
            self.loop = GLib.MainLoop()
            bus = self.pipe.get_bus()
            bus.add_signal_watch()
            bus.connect("message::error", self._on_bus_error)
            bus.connect("message::element", self._on_bus_element)

            if self._stopping.is_set():
                return  # stop() raced the setup; nothing started yet

            self.pipe.set_state(Gst.State.PLAYING)

            # open signaling websocket (sync) in a helper thread
            signed = sign_wss_url(self.cfg, self.client_id)
            self.ws = websocket.WebSocketApp(
                signed,
                on_open=self._on_ws_open,
                on_message=self._on_ws_msg,
                on_error=lambda ws, e: _LOG.warning("[%s] wss err: %s", dev, e),
                on_close=lambda ws, *a: _LOG.info("[%s] wss closed", dev),
            )
            threading.Thread(target=self.ws.run_forever, daemon=True).start()

            if self._stopping.is_set():
                return  # stop() raced the handshake; _teardown closes the ws
            self.loop.run()
        except Exception as err:  # noqa: BLE001
            _LOG.exception("[%s] session crashed: %s", dev, err)

    def _on_bus_element(self, bus, msg):
        """`level` meters: RMS of the camera's decoded audio ("camera") and of
        the mix that is published ("mix"), in dBFS, for /health. A camera value
        with a mix value 40 dB lower means the mixer is dropping the camera's
        audio (timestamps outside AUDIO_MIX_LATENCY_MS)."""
        st = msg.get_structure()
        if st is None or st.get_name() != "level":
            return
        rms = st.get_value("rms")
        if rms:
            name = msg.src.get_name() if msg.src else ""
            key = "camera" if name == "clevel" else "mix"
            self.audio_rms_db[key] = round(float(rms[0]), 1)

    def _on_bus_error(self, bus, msg):
        err, debug = msg.parse_error()
        _LOG.warning("[%s] pipeline error from %s: %s (%s); ending session",
                     self.device_id, msg.src.get_name() if msg.src else "?",
                     err.message, (debug or "").splitlines()[0] if debug else "")
        if self.loop:
            self.loop.quit()

    # ---- webrtcbin: create & send offer ----
    def _on_neg(self, webrtc):
        promise = Gst.Promise.new_with_change_func(self._on_offer_created, webrtc, None)
        webrtc.emit("create-offer", None, promise)

    def _on_offer_created(self, promise, webrtc, _):
        promise.wait()
        reply = promise.get_reply()
        offer = reply.get_value("offer")
        p2 = Gst.Promise.new()
        webrtc.emit("set-local-description", offer, p2)
        p2.interrupt()
        sdp_text = offer.sdp.as_text()
        self._ws_send("SDP_OFFER", json.dumps({"type": "offer", "sdp": sdp_text}))
        _LOG.info("[%s] sent SDP offer", self.device_id)

    # ---- incoming signaling ----
    def _on_ws_msg(self, ws, raw):
        try:
            if isinstance(raw, (bytes, bytearray)):
                raw = raw.decode("utf-8", "ignore")
            msg = json.loads(raw)
        except Exception:
            return
        action = (msg.get("messageType") or msg.get("action") or "").upper()
        b64 = msg.get("messagePayload")
        if not b64:
            return
        payload = base64.b64decode(b64).decode("utf-8", "ignore")
        if action in ("SDP_ANSWER", "ANSWER"):
            GLib.idle_add(self._apply_answer, payload)
        elif action in ("ICE_CANDIDATE", "CANDIDATE"):
            GLib.idle_add(self._apply_ice, payload)

    def _apply_answer(self, payload: str):
        try:
            obj = json.loads(payload) if payload.strip().startswith("{") else {"sdp": payload}
            sdp = obj.get("sdp", payload)
            _, msg = GstSdp.SDPMessage.new_from_text(sdp)
            answer = GstWebRTC.WebRTCSessionDescription.new(
                GstWebRTC.WebRTCSDPType.ANSWER, msg
            )
            p = Gst.Promise.new()
            self.webrtc.emit("set-remote-description", answer, p)
            p.interrupt()
            _LOG.info("[%s] applied SDP answer", self.device_id)
            if not self._rtcp_fb_logged:
                # What the camera agreed to: "nack" = retransmission, "nack pli"
                # = it will send a new keyframe when asked. Absent lines mean the
                # loss handling below falls back to PLI/wait-for-keyframe only.
                self._rtcp_fb_logged = True
                fb = sorted({ln.strip() for ln in sdp.splitlines()
                             if ln.startswith("a=rtcp-fb:")})
                self.nack_negotiated = any(ln.endswith(" nack") for ln in fb)
                _LOG.info("[%s] answer rtcp-fb: %s", self.device_id,
                          ", ".join(fb) if fb else "(none)")
        except Exception as err:  # noqa: BLE001
            _LOG.warning("[%s] answer err: %s", self.device_id, err)
        return False

    def _apply_ice(self, payload: str):
        try:
            obj = json.loads(payload)
            self.webrtc.emit("add-ice-candidate", obj.get("sdpMLineIndex", 0),
                             obj.get("candidate", ""))
        except Exception as err:  # noqa: BLE001
            _LOG.warning("[%s] ice err: %s", self.device_id, err)
        return False

    def _on_local_ice(self, webrtc, mline, candidate):
        self._ws_send("ICE_CANDIDATE", json.dumps({
            "candidate": candidate, "sdpMLineIndex": mline, "sdpMid": str(mline),
        }))

    def _on_ws_open(self, ws):
        # Flush under the lock and only then mark ready: _on_local_ice fires
        # from a GStreamer thread, and a candidate sent before the queued
        # SDP_OFFER is discarded by KVS.
        with self._ws_lock:
            queued, self._ws_queue = self._ws_queue, []
            _LOG.info("[%s] wss open (flushing %d queued)", self.device_id,
                      len(queued))
            for m in queued:
                try:
                    ws.send(m)
                except Exception as err:  # noqa: BLE001
                    _LOG.warning("[%s] ws flush err: %s", self.device_id, err)
            self._ws_ready = True

    def _ws_send(self, action: str, payload: str):
        msg = json.dumps({
            "action": action,
            "messagePayload": base64.b64encode(payload.encode()).decode(),
        })
        with self._ws_lock:
            if not self._ws_ready:
                self._ws_queue.append(msg)  # hold until the socket is open
                return
            try:
                self.ws.send(msg)
            except Exception as err:  # noqa: BLE001
                _LOG.warning("[%s] ws send err: %s", self.device_id, err)

    # ---- incoming media -> matroskamux -> fdsink -> ffmpeg -> RTSP publish ----
    def _on_pad(self, webrtc, pad):
        """Every webrtcbin src pad must be linked before this handler returns:
        the first RTP buffer is pushed right after pad-added, and a push on an
        unlinked pad is a fatal not-linked error for the source (measured: the
        audio pad arrives ~1 ms after the video pad, and waiting for it killed
        every session). So the whole chain, including the muxer's request pad
        for a track that has not arrived yet, is built on the FIRST pad; later
        pads just link in. If a wanted track never arrives, _on_settle_timeout
        releases its muxer pad so the header can be written without it."""
        if pad.direction != Gst.PadDirection.SRC:
            return
        caps = pad.get_current_caps()
        name = caps.to_string() if caps else ""
        # Note whether the SDP carried SPS/PPS: with them h264parse can put the
        # parameter sets in front of the very first keyframe (see VIDEO_BRANCH).
        _LOG.info("[%s] pad-added: %s%s", self.device_id, name[:80],
                  " [sprop-parameter-sets present]" if "sprop-parameter-sets" in name else "")
        kind = "audio" if "audio" in name else "video" if "video" in name else None
        if kind is None:
            return
        if self._stopping.is_set():
            return
        if kind == "audio" and not self.audio:
            return  # no audio transceiver was requested; nothing should arrive
        with self._pad_lock:
            if kind in self._pads:
                _LOG.warning("[%s] ignoring duplicate %s pad", self.device_id, kind)
                return
            if not self._chain_built and not self._build_chain():
                return
            chain = self._chain
            if chain is None:
                return  # the build failed on the other pad's thread
            ret = pad.link(chain.get_static_pad(f"{kind}_sink"))
            if ret != Gst.PadLinkReturn.OK:
                _LOG.error("[%s] %s pad link failed: %s", self.device_id, kind, ret)
                return
            self._pads[kind] = pad
            if kind == "video" and not self.started:
                # The stale clock starts now, not at the epoch: before this a
                # freshly linked session was judged stale on the watchdog's next
                # tick ("no frames 1794449565.4s"), burning a reconnect attempt
                # before its first keyframe could arrive.
                self.last_sample_ts = time.time()
                self.started = True
                self.started_evt.set()
            want = {"video", "audio"} if self.audio else {"video"}
            if want <= set(self._pads):
                if self._settle_id:
                    GLib.source_remove(self._settle_id)
                    self._settle_id = 0
            elif not self._settle_id:
                self._settle_id = GLib.timeout_add(PAD_SETTLE_MS, self._on_settle_timeout)

    def _on_settle_timeout(self):
        self._settle_id = 0
        if self._stopping.is_set() or not self._chain_built or self._chain is None:
            return False
        with self._pad_lock:
            self._settle_locked()
        return False

    def _settle_locked(self) -> None:
        if "audio" not in self._pads and self.audio:
            # The silence source already feeds the mixer, so the stream is
            # publishing with a (silent) audio track; a late audio pad simply
            # joins the mix when it arrives.
            _LOG.warning("[%s] audio pad did not arrive within %d ms; audio is silence until it does",
                         self.device_id, PAD_SETTLE_MS)
        if "video" not in self._pads:
            _LOG.warning("[%s] no video pad within %d ms of the first pad",
                         self.device_id, PAD_SETTLE_MS)

    def _build_chain(self) -> bool:
        """Spawn ffmpeg and create mpegtsmux -> fdsink plus a branch (with a
        ghost sink pad) for every expected track. Returns False on failure."""
        self._chain_built = True
        try:
            proc = subprocess.Popen(self._ffmpeg_cmd(audio=self.audio),
                                    stdin=subprocess.PIPE, stderr=subprocess.PIPE)
        except Exception as err:  # noqa: BLE001
            # ENOMEM / EAGAIN at pids_limit: the session never reaches
            # `started`, so the watchdog respawns it.
            _LOG.error("[%s] could not start ffmpeg: %s", self.device_id, err)
            return False
        with self._ffmpeg_lock:
            self.ffmpeg = proc
        fd = proc.stdin.fileno()
        _grow_pipe(fd)
        threading.Thread(target=self._relay_ffmpeg_stderr, args=(proc,),
                         daemon=True).start()
        try:
            chain = Gst.parse_bin_from_description(build_chain(fd, self.audio), False)
        except GLib.Error as err:
            _LOG.error("[%s] chain parse failed: %s", self.device_id, err)
            self._kill_ffmpeg()
            return False
        self._chain = chain
        self.pipe.add(chain)
        for kind, depay_name in (("video", "vdepay"), ("audio", "adepay")):
            depay = chain.get_by_name(depay_name)  # Gst.Iterator is not Python-iterable
            if depay is None:
                continue
            ghost = Gst.GhostPad.new(f"{kind}_sink", depay.get_static_pad("sink"))
            ghost.set_active(True)
            chain.add_pad(ghost)
        # Heartbeat and byte counters: buffers reaching fdsink are what ffmpeg
        # actually gets; buffers leaving the branch queues count per track.
        chain.get_by_name("out").get_static_pad("sink").add_probe(
            OUT_PROBE_TYPE, self._on_out_buffer, None)
        chain.get_by_name("vdepay").get_static_pad("src").add_probe(
            Gst.PadProbeType.BUFFER, self._on_au, "video")
        chain.get_by_name("vq").connect("overrun", self._on_video_overrun)
        # The mixed/encoded audio is what enters the muxer: gate it on the
        # first video keyframe there (see _on_mix_out).
        chain.get_by_name("aenc").get_static_pad("src").add_probe(
            Gst.PadProbeType.BUFFER, self._on_mix_out, None)
        chain.get_by_name("aq").connect("overrun", self._on_audio_overrun)
        if self.audio:
            chain.get_by_name("adepay").get_static_pad("src").add_probe(
                Gst.PadProbeType.BUFFER, self._on_au, "audio")
            chain.get_by_name("aqin").connect("overrun", self._on_audio_overrun)
        chain.sync_state_with_parent()
        _LOG.info("[%s] publishing via ffmpeg pid %d (audio=%s) -> %s",
                  self.device_id, proc.pid, self.audio, self.publish_url)
        return True

    def _on_out_buffer(self, pad, info, _):
        self.last_sample_ts = time.time()  # heartbeat: data is reaching ffmpeg
        return Gst.PadProbeReturn.OK

    # ---- arrival-time stamping (see the comment above VIDEO_BRANCH) ----
    def _running_time(self) -> int:
        pipe = self.pipe
        if pipe is None:  # probes can still fire while the pipeline is torn down
            return Gst.CLOCK_TIME_NONE
        clock = pipe.get_clock()
        if clock is None:
            return Gst.CLOCK_TIME_NONE
        return clock.get_time() - pipe.get_base_time()

    def _on_new_jitterbuffer(self, rtpbin, jb, session, ssrc):
        self._arrival[ssrc] = {}
        self._probe_nicesrc_once()
        jb.get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER, self._on_rtp_in, ssrc)
        jb.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, self._on_rtp_out, ssrc)
        _LOG.info("[%s] jitterbuffer for ssrc %u: arrival-time stamping on", self.device_id, ssrc)

    @staticmethod
    def _rtp_seq(buf) -> int | None:
        ok, m = buf.map(Gst.MapFlags.READ)
        if not ok:
            return None
        try:
            if m.size < 4:
                return None
            d = m.data
            return (d[2] << 8) | d[3]
        finally:
            buf.unmap(m)

    def _probe_nicesrc_once(self) -> None:
        """Find webrtcbin's nicesrc element(s) and histogram their output."""
        if self._nicesrc_probed or self.webrtc is None:
            return
        self._nicesrc_probed = True
        it = self.webrtc.iterate_recurse()
        while True:
            res, elem = it.next()
            if res == Gst.IteratorResult.DONE:
                break
            if res != Gst.IteratorResult.OK:
                continue
            if elem.get_factory() and elem.get_factory().get_name() == "nicesrc":
                pad = elem.get_static_pad("src")
                if pad is not None:
                    pad.add_probe(Gst.PadProbeType.BUFFER | Gst.PadProbeType.BUFFER_LIST,
                                  self._on_nicesrc_buf, None)
                    _LOG.info("[%s] diagnostics probe on %s", self.device_id, elem.get_name())

    def _on_nicesrc_buf(self, pad, info, _):
        self._hist["nicesrc"].tick(self._running_time())
        return Gst.PadProbeReturn.OK

    def _on_rtp_in(self, pad, info, ssrc):
        now = self._running_time()
        self._hist["jb_in"].tick(now)
        seq = self._rtp_seq(info.get_buffer())
        if seq is not None:
            table = self._arrival.get(ssrc)
            if table is not None:
                if len(table) > 4096:  # packets that never came out (lost for good)
                    table.clear()
                table.setdefault(seq, now)  # first arrival wins (RTX later)
        return Gst.PadProbeReturn.OK

    def _on_rtp_out(self, pad, info, ssrc):
        buf = info.get_buffer()
        seq = self._rtp_seq(buf)
        table = self._arrival.get(ssrc)
        arrival = table.pop(seq, None) if (seq is not None and table is not None) else None
        if arrival is not None and arrival != Gst.CLOCK_TIME_NONE:
            buf.pts = arrival
            buf.dts = arrival
            self._restamped += 1
        else:
            self._unstamped += 1
        return Gst.PadProbeReturn.OK

    def _on_au(self, pad, info, kind):
        """Per track: count bytes and replace the (arrival-time) PTS with the
        smoothed, monotonic clock from _Pll. Video is gated on the first
        keyframe: the stream must open with SPS/PPS/IDR."""
        buf = info.get_buffer()
        self._last_au_ts[kind] = time.time()
        if kind == "video" and not self._kf_seen:
            if buf.has_flags(Gst.BufferFlags.DELTA_UNIT):
                # The camera does not start a session on an IDR, and
                # rtph264depay's wait-for-keyframe only applies after packet
                # loss. Mid-GOP frames are useless to the decoder and, in an
                # MPEG-TS, keep ffmpeg's probe from finding the picture size
                # ("Could not find codec parameters ... unspecified size", two
                # sessions in a row on 2026-11-12). Drop them and ask upstream
                # (rtpsession -> PLI) for a keyframe, once per second.
                self._pre_kf_dropped += 1
                now = time.monotonic()
                if now - self._last_kf_request >= 1.0:
                    self._last_kf_request = now
                    # send_event on the depayloader's SRC pad travels upstream
                    # through it (push_event would go downstream to the muxer)
                    pad.send_event(GstVideo.video_event_new_upstream_force_key_unit(
                        Gst.CLOCK_TIME_NONE, True, 0))
                return Gst.PadProbeReturn.DROP
            self._kf_seen = True
            if self._pre_kf_dropped:
                _LOG.info("[%s] first keyframe after dropping %d mid-GOP frames",
                          self.device_id, self._pre_kf_dropped)
        pts = buf.pts
        pll = self._pll[kind]
        if pts == Gst.CLOCK_TIME_NONE:
            # Should not happen (every RTP packet is re-stamped); never let an
            # untimed buffer reach the muxer as a duplicate timecode.
            self._au_no_pts += 1
            pts = pll.pts if pll.pts is not None else self._running_time()
        smooth = pll.update(pts)
        buf.pts = smooth
        buf.dts = smooth
        size = buf.get_size()
        if kind == "video":
            self.video_bytes += size
        else:
            self.audio_bytes += size
            self.audio_active = True
        return Gst.PadProbeReturn.OK

    def _on_mix_out(self, pad, info, _):
        """Mixed, re-encoded audio on its way into the muxer. No audio before
        the first video keyframe: the muxer emits in DTS order, so audio
        (here: the silence source, running from the first instant) would come
        out first and alone, ffmpeg's probe window (analyzeduration, measured
        on the stream that has advanced furthest) would close on audio only
        and the session die with "Could not find codec parameters"
        (2026-11-12)."""
        if not self._kf_seen:
            self._pre_kf_audio_dropped += 1
            return Gst.PadProbeReturn.DROP
        return Gst.PadProbeReturn.OK

    def _on_video_overrun(self, queue):
        self.video_drops += 1  # ffmpeg is not draining; the queue dropped a buffer

    def _on_audio_overrun(self, queue):
        self.audio_drops += 1

    def _ffmpeg_cmd(self, audio: bool) -> list[str]:
        """ffmpeg reads the MPEG-TS stream on stdin (video + optional Opus
        audio, timestamps from the jitterbuffer), copies the video, decodes/
        boosts/AAC-encodes the audio and publishes RTSP to MediaMTX.
        -max_interleave_delta bounds how long the muxer may hold one stream
        waiting for the other (default 10 s)."""
        # No `-fflags +nobuffer`: with the Matroska demuxer it dropped a third
        # of the video frames in the in-image test (59 of 90).
        # Probe bounds. MPEG-TS has no header, so ffmpeg always reads the whole
        # analyzeduration before it starts publishing (measured: 0.5 s -> 0.5 s,
        # 4 s -> 4 s). This IS the start-up delay, so keep it short; it is safe
        # because the muxer only starts once both tracks are present and the
        # video opens on SPS/PPS/IDR (VIDEO_BRANCH). probesize must cover that
        # second of video at the camera's ~2.5 Mbit/s with room to spare.
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin",
               "-thread_queue_size", "512",
               "-analyzeduration", "1000000", "-probesize", "2000000",
               "-f", "mpegts", "-i", "pipe:0", "-map", "0:v", "-c:v", "copy"]
        # The TS always carries one audio track (the mixer's output, silence
        # when the camera sends none or `audio` is False), so MediaMTX's track
        # count never changes under its readers.
        # aresample=async=1: keeps the output clock monotonic across the
        # occasional backward jump instead of ffmpeg rewriting timestamps.
        del audio  # kept in the signature for the callers and tests
        cmd += ["-map", "0:a",
                "-filter:a", "aresample=async=1:min_hard_comp=0.100:first_pts=0,"
                             f"volume={AUDIO_GAIN_DB}dB,alimiter=limit=0.9:level=disabled",
                "-c:a", "aac", "-b:a", "64k"]
        cmd += ["-max_interleave_delta", "1000000",
                "-f", "rtsp", "-rtsp_transport", "tcp", self.publish_url]
        return cmd

    def _relay_ffmpeg_stderr(self, proc) -> None:
        """Prefix ffmpeg's warnings with the device id and a timestamp (they
        used to land in the log bare, unattributable with two cameras). Ends on
        EOF when the child exits; never blocks shutdown."""
        try:
            for line in proc.stderr:
                text = line.decode("utf-8", "replace").rstrip()
                if text:
                    _LOG.warning("[%s] ffmpeg[%d]: %s", self.device_id, proc.pid, text)
        except Exception:
            pass
        rc = proc.poll()
        if rc is None:
            try:
                rc = proc.wait(timeout=5)
            except Exception:
                rc = None
        # A negative code is the signal that killed it; 0 means ffmpeg saw EOF
        # on its stdin, i.e. nobody was writing to it any more.
        _LOG.info("[%s] ffmpeg[%d] exited rc=%s", self.device_id, proc.pid, rc)


# --------------------------------------------------------------------------- #
# HTTP control API
# --------------------------------------------------------------------------- #
SESSIONS: dict[str, GstSession] = {}
SESSIONS_LOCK = threading.Lock()


def _stop_session(sess: GstSession, timeout: float = STOP_TIMEOUT_SEC) -> None:
    """Run sess.stop() on a helper thread and wait at most `timeout` seconds.
    A stop still running afterwards finishes in the background (daemon thread)
    instead of blocking the caller."""
    t = threading.Thread(target=sess.stop, daemon=True,
                         name=f"stop-{sess.device_id}")
    t.start()
    t.join(timeout)
    if t.is_alive():
        _LOG.warning("[%s] stop() still running after %.0fs; continuing without it",
                     sess.device_id, timeout)


def _spawn_session(device_id, cfg, pub, read_url, attempts=0, audio=None) -> GstSession:
    # Audio only on fresh/early sessions; after repeated failures fall back to
    # video-only so audio can never keep video from coming up. `audio` forces
    # the choice (an HA-initiated retry after give-up is not an audio problem).
    use_audio = ENABLE_AUDIO and (attempts < AUDIO_FALLBACK_AFTER if audio is None else audio)
    if ENABLE_AUDIO and not use_audio:
        _LOG.info("[%s] audio disabled for this attempt (fallback to video-only)",
                  device_id)
    sess = GstSession(device_id, cfg, pub, read_url, audio=use_audio)
    sess.reconnect_attempts = attempts
    SESSIONS[device_id] = sess
    sess.start()
    return sess


async def handle_start(request: web.Request) -> web.Response:
    if not authorized(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    device_id = request.match_info["device_id"]
    if not DEVICE_ID_RE.fullmatch(device_id):
        return web.json_response({"error": "invalid device_id"}, status=400)
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"error": "expected JSON"}, status=400)
    # Validate BEFORE touching the running session: a malformed body used to
    # stop the healthy session and then 500, leaving the camera with nothing
    # for the watchdog to heal.
    try:
        cfg = parse_live_config(payload)
    except (KeyError, AttributeError, TypeError) as err:
        return web.json_response({"error": f"bad liveConfig: {err!r}"}, status=400)
    bad = check_endpoints(cfg)
    if bad:
        _LOG.warning("[%s] start refused: %s", device_id, bad)
        return web.json_response({"error": bad}, status=400)
    read_url = f"{RTSP_READ_BASE.rstrip('/')}/telus_{device_id}"
    pub = f"{RTSP_PUBLISH_BASE.rstrip('/')}/telus_{device_id}"

    # Reuse ONLY a session that is actually healthy (video flowing) or still in
    # its initial handshake. A session whose thread is "alive" but has gone
    # stale (camera dropped it) is NOT reused -- we restart it below with the
    # fresh credentials HA just sent. This is what fixes "works sometimes" and
    # "have to reload every ~10 min".
    spawn_attempts = 0
    with SESSIONS_LOCK:
        existing = SESSIONS.get(device_id)
        if existing and existing._thread.is_alive():
            # Always take HA's fresh credentials: the watchdog's auto-reconnects
            # use `cfg`, and until 2026-11-12 they kept the original (expiring)
            # creds while every refresh HA sent was thrown away.
            existing.cfg = cfg
            if existing.is_healthy():
                existing.reconnect_attempts = 0  # flowing: a clean slate
                _LOG.info("[%s] start: reusing healthy session, credentials refreshed", device_id)
                return web.json_response({"rtsp_url": read_url, "reused": True, "started": True})
            if existing.is_connecting():
                # Still in its handshake grace; the attempt counter is NOT
                # reset here (that made a dead camera retry forever).
                _LOG.info("[%s] start: reusing connecting session, credentials refreshed", device_id)
                return web.json_response({"rtsp_url": read_url, "reused": True, "started": False})
            if existing.reconnect_attempts >= MAX_AUTO_RECONNECTS:
                age = time.time() - existing.created_ts
                if age < RESTART_BACKOFF_SEC:
                    _LOG.info("[%s] start: gave up %.0fs ago; next attempt in %.0fs",
                              device_id, age, RESTART_BACKOFF_SEC - age)
                    return web.json_response({"rtsp_url": read_url, "reused": True,
                                              "started": False, "backoff": True})
                # One HA-initiated attempt per backoff period; it inherits the
                # give-up state so the watchdog does not add six more.
                spawn_attempts = MAX_AUTO_RECONNECTS
        if existing is None and len(SESSIONS) >= MAX_SESSIONS:
            return web.json_response({"error": "too many sessions"}, status=429)
        old = SESSIONS.pop(device_id, None)
    if old:
        # Off the event loop: a blocking stop here is what froze /health and
        # /start for 27 h on 2026-11-04.
        await asyncio.get_running_loop().run_in_executor(None, _stop_session, old)

    with SESSIONS_LOCK:
        sess = _spawn_session(device_id, cfg, pub, read_url, attempts=spawn_attempts,
                              audio=True if spawn_attempts else None)
        if old is not None:
            sess.reconnects_total = old.reconnects_total
    # Wait (off the event loop) until the video pad is linked, at most 12 s.
    await asyncio.get_running_loop().run_in_executor(None, sess.started_evt.wait, 12)
    _LOG.info("[%s] start returned (started=%s)", device_id, sess.started)
    return web.json_response({"rtsp_url": read_url, "started": sess.started})


def _reap_stopped_sessions() -> None:
    """Forget sessions that are fully gone; kill the ffmpeg of any session that
    was stopped but whose thread is still around (a wedged pipeline teardown
    must never keep a child process, its pipes or a writer thread alive)."""
    with _ALL_SESSIONS_LOCK:
        sessions = list(_ALL_SESSIONS)
    now = time.time()
    gone = []
    for s in sessions:
        if s._stop_ts and (now - s._stop_ts) > STOP_TIMEOUT_SEC and s.ffmpeg_alive():
            _LOG.warning("[%s] stopped %.0fs ago but ffmpeg still alive; reaping",
                         s.device_id, now - s._stop_ts)
            s._kill_ffmpeg()
        if not s._thread.is_alive() and not s.ffmpeg_alive():
            gone.append(s)
    if gone:
        with _ALL_SESSIONS_LOCK:
            for s in gone:
                try:
                    _ALL_SESSIONS.remove(s)
                except ValueError:
                    pass


def _process_metrics() -> dict:
    """Leak indicators for /health: before 2026-11-12 these would have read
    803 fds / 92 threads / 18 ffmpeg children after 9 h of uptime."""
    try:
        fds = len(os.listdir("/proc/self/fd"))
    except OSError:
        fds = None
    with _ALL_SESSIONS_LOCK:
        sessions = list(_ALL_SESSIONS)
    now = time.time()
    return {
        "fds": fds,
        "threads": threading.active_count(),
        "ffmpeg_children": sum(1 for s in sessions if s.ffmpeg_alive()),
        "session_objects": len(sessions),
        # stopped > 60 s ago and the session thread is still alive: a wedged
        # webrtcbin teardown (set_state(NULL) never returned).
        "wedged_sessions": sum(
            1 for s in sessions
            if s._stop_ts and (now - s._stop_ts) > 60 and s._thread.is_alive()
        ),
    }


def _watchdog() -> None:
    """Heal sessions without waiting for HA: if video has stopped flowing,
    restart the session using the stored (still-valid) credentials. After
    MAX_AUTO_RECONNECTS consecutive failures (e.g. creds finally expired), back
    off and let HA's next /start supply fresh credentials.
    """
    global WATCHDOG_LAST_TICK
    while True:
        time.sleep(WATCHDOG_INTERVAL_SEC)
        WATCHDOG_LAST_TICK = time.time()
        try:
            _reap_stopped_sessions()
            with SESSIONS_LOCK:
                items = list(SESSIONS.items())
            now = time.time()
            for dev, sess in items:
                if sess.is_healthy():
                    sess.reconnect_attempts = 0  # flowing again -> reset counter
                    continue
                if sess.is_connecting():
                    continue  # still in handshake grace
                # Dead in one of three ways: the thread exited (setup crash or
                # pipeline error), it never produced a frame within the grace
                # (signaling/ICE/ffmpeg failure -- previously skipped forever),
                # or it started and then went quiet past the stale window.
                if not sess._thread.is_alive():
                    why = "thread exited"
                elif not sess.started:
                    why = f"never started ({now - sess.created_ts:.0f}s)"
                elif (now - sess.last_sample_ts) > STREAM_STALE_SEC:
                    why = f"no frames {now - sess.last_sample_ts:.1f}s"
                else:
                    continue
                if sess.reconnect_attempts >= MAX_AUTO_RECONNECTS:
                    if not sess._gave_up_logged:
                        sess._gave_up_logged = True
                        _LOG.warning("[%s] gave up after %d/%d auto-reconnects (%s); "
                                     "stream stays dark until Home Assistant re-opens "
                                     "the camera with fresh credentials",
                                     dev, sess.reconnect_attempts, MAX_AUTO_RECONNECTS, why)
                    continue  # give up until a fresh /start resets us
                attempts = sess.reconnect_attempts + 1
                _LOG.info("[%s] stale (%s) -> auto-reconnect %d/%d",
                          dev, why, attempts, MAX_AUTO_RECONNECTS)
                cfg, pub, read_url = sess.cfg, sess.publish_url, sess.read_url
                _stop_session(sess)
                with SESSIONS_LOCK:
                    if SESSIONS.get(dev) is sess:  # not replaced by HA meanwhile
                        new = _spawn_session(dev, cfg, pub, read_url, attempts=attempts)
                        new.reconnects_total = sess.reconnects_total + 1
        except Exception as err:  # noqa: BLE001
            _LOG.warning("watchdog error: %s", err)


WATCHDOG_LAST_TICK = 0.0


async def handle_stop(request: web.Request) -> web.Response:
    if not authorized(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    device_id = request.match_info["device_id"]
    if not DEVICE_ID_RE.fullmatch(device_id):
        return web.json_response({"error": "invalid device_id"}, status=400)
    with SESSIONS_LOCK:
        sess = SESSIONS.pop(device_id, None)
    if sess:
        await asyncio.get_running_loop().run_in_executor(None, _stop_session, sess)
    return web.json_response({"stopped": bool(sess)})


async def handle_health(request: web.Request) -> web.Response:
    """200 while the service itself is sound. 503 (so the Docker healthcheck
    fires) only for process sickness a restart would cure: the watchdog thread
    dead or a wedged session for more than 5 minutes. A camera outage is NOT
    a reason to restart the bridge; it is reported as `degraded` instead."""
    now = time.time()
    sessions = {}
    reasons = []
    for dev, s in list(SESSIONS.items()):
        sessions[dev] = {
            "healthy": s.is_healthy(),
            "started": s.started,
            "secs_since_frame": round(now - s.last_sample_ts, 1)
            if s.last_sample_ts else None,
            "reconnect_attempts": s.reconnect_attempts,
            "reconnects_total": s.reconnects_total,
            "gave_up": s.reconnect_attempts >= MAX_AUTO_RECONNECTS and not s.is_healthy(),
            "nack_negotiated": s.nack_negotiated,
            "audio_requested": s.audio,
            "audio_fallback": ENABLE_AUDIO and not s.audio,
            "audio_active": s.audio_active,
            # "camera" once the camera's audio has been heard this session,
            # else "silence" (the track exists either way)
            "audio_source": "camera" if s.audio_active else "silence",
            "audio_rms_db": s.audio_rms_db,
            "audio_bytes": s.audio_bytes,
            "video_bytes": s.video_bytes,
            "rtp_restamped": s._restamped,
            "rtp_unstamped": s._unstamped,
            "au_without_pts": s._au_no_pts,
            "pre_keyframe_dropped": s._pre_kf_dropped,
            "pre_keyframe_audio_dropped": s._pre_kf_audio_dropped,
            # which track starved tells a mux stall from a camera outage
            "last_au_age_s": {k: (round(now - t, 1) if t else None)
                              for k, t in s._last_au_ts.items()},
            "arrival_hist": {k: h.as_dict() for k, h in s._hist.items()},
            "video_drops": s.video_drops,
            "audio_drops": s.audio_drops,
        }
        if s.started and not s.is_healthy():
            reasons.append(f"{dev}: no video for {now - s.last_sample_ts:.0f}s")
        if sessions[dev]["gave_up"]:
            reasons.append(f"{dev}: gave up reconnecting; needs a fresh /start")
    proc = _process_metrics()
    watchdog_age = now - WATCHDOG_LAST_TICK if WATCHDOG_LAST_TICK else 0.0
    sick = []
    if WATCHDOG_LAST_TICK and watchdog_age > 3 * WATCHDOG_INTERVAL_SEC:
        sick.append(f"watchdog thread silent for {watchdog_age:.0f}s")
    if any(s._stop_ts and (now - s._stop_ts) > 300 and s._thread.is_alive()
           for s in list(_ALL_SESSIONS)):
        sick.append("session wedged in teardown for > 5 min")
    proc["watchdog_age_s"] = round(watchdog_age, 1)
    body = {"ok": not sick, "degraded": bool(reasons) or bool(sick),
            "reasons": sick + reasons, "version": BRIDGE_VERSION,
            "sessions": sessions, "process": proc}
    return web.json_response(body, status=503 if sick else 200)


class _NoHealthAccessLog(logging.Filter):
    """The 30 s Docker healthcheck was more than half of the log."""
    def filter(self, record: logging.LogRecord) -> bool:
        return "GET /health" not in record.getMessage()


def make_app() -> web.Application:
    logging.getLogger("aiohttp.access").addFilter(_NoHealthAccessLog())
    app = web.Application()
    app.router.add_post("/session/{device_id}/start", handle_start)
    app.router.add_post("/session/{device_id}/stop", handle_stop)
    app.router.add_get("/health", handle_health)
    return app


if __name__ == "__main__":
    _LOG.info("TELUS GStreamer bridge %s on %s:%d  publish=%s read=%s",
              BRIDGE_VERSION, BRIDGE_HOST, LISTEN_PORT, RTSP_PUBLISH_BASE, RTSP_READ_BASE)
    from urllib.parse import urlparse
    if urlparse(RTSP_READ_BASE).hostname in ("127.0.0.1", "localhost", "mediamtx"):
        _LOG.warning("RTSP_READ_BASE=%s is only reachable from this host; if Home "
                     "Assistant runs elsewhere set LAN_IP in .env", RTSP_READ_BASE)
    _LOG.info("session API %s; signaling endpoints must match %s",
              "requires X-Bridge-Token" if BRIDGE_TOKEN else "is unauthenticated (set BRIDGE_TOKEN)",
              ENDPOINT_ALLOW_RE.pattern)
    threading.Thread(target=_watchdog, daemon=True).start()
    _LOG.info("watchdog started (stale>%ss, grace=%ss, max_reconnects=%d, stop_timeout=%ss, "
              "nack=%s, jitter=%dms)",
              STREAM_STALE_SEC, STARTUP_GRACE_SEC, MAX_AUTO_RECONNECTS, STOP_TIMEOUT_SEC,
              WEBRTC_NACK, WEBRTC_JITTER_MS)
    # `docker kill -s USR1 <bridge container>` dumps every thread's stack to
    # stderr (docker logs), so a future hang can be located instead of guessed.
    faulthandler.register(signal.SIGUSR1, all_threads=True)
    web.run_app(make_app(), host=BRIDGE_HOST, port=LISTEN_PORT)

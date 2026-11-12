#!/usr/bin/env python3
"""TELUS KVS-WebRTC -> RTSP bridge using GStreamer webrtcbin.

aiortc could complete the KVS handshake but never received media. GStreamer's
webrtcbin (libnice ICE + libwebrtc-grade media) handles it properly. The HA
integration POSTs the per-view live config (channel ARN + region + temp AWS
creds + signaling endpoints); this service does the KVS viewer handshake with
webrtcbin and republishes the received H.264 to MediaMTX over RTSP (no
re-encode — rtph264depay -> h264parse -> rtspclientsink).

HTTP control API (unchanged from the aiortc version):
  POST /session/{device_id}/start   body {deviceId, liveConfig}
  POST /session/{device_id}/stop
  GET  /health
"""
from __future__ import annotations

import asyncio
import base64
import faulthandler
import json
import os
import select
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
gi.require_version("GstWebRTC", "1.0")
gi.require_version("GstSdp", "1.0")
from gi.repository import Gst, GstWebRTC, GstSdp, GLib  # noqa: E402

Gst.init(None)

import logging
logging.basicConfig(level=logging.INFO)
_LOG = logging.getLogger("telus-gst-bridge")

RTSP_PUBLISH_BASE = os.environ.get("RTSP_PUBLISH_BASE", "rtsp://mediamtx:8554")
RTSP_READ_BASE = os.environ.get("RTSP_READ_BASE", "rtsp://mediamtx:8554")
LISTEN_PORT = int(os.environ.get("BRIDGE_PORT", "8787"))

# Self-healing tuning. The camera caps a live session (~10 min) and then drops
# it; the bridge must notice and restart instead of serving a dead session.
STREAM_STALE_SEC = float(os.environ.get("STREAM_STALE_SEC", "8"))    # no frames => stale
STARTUP_GRACE_SEC = float(os.environ.get("STARTUP_GRACE_SEC", "25"))  # handshake window
WATCHDOG_INTERVAL_SEC = float(os.environ.get("WATCHDOG_INTERVAL_SEC", "3"))
MAX_AUTO_RECONNECTS = int(os.environ.get("MAX_AUTO_RECONNECTS", "6"))  # before waiting for HA
# How long a caller waits for a session's stop() before leaving it to finish in
# the background. Tearing down a wedged webrtcbin can block indefinitely.
STOP_TIMEOUT_SEC = float(os.environ.get("STOP_TIMEOUT_SEC", "5"))
# ffmpeg child shutdown: its stdin pipes are closed first (EOF makes it finish
# on its own), then SIGTERM, then SIGKILL after this grace. Before 2026-11-12 the
# bridge only sent SIGTERM and never closed the pipes; ffmpeg 5.1 blocked in
# read() on the video pipe ignores that flag, so every stale-session restart
# left one ffmpeg (and one audio-writer thread blocked on its full pipe) behind:
# 17 leaked ffmpegs and 803 of 1024 fds after 9 h.
FFMPEG_KILL_GRACE_SEC = float(os.environ.get("FFMPEG_KILL_GRACE_SEC", "3"))
# A pipe write that does not complete within this long means ffmpeg has stopped
# reading; the data is dropped instead of blocking a GStreamer streaming thread
# (video) or the audio writer forever.
PIPE_WRITE_TIMEOUT_SEC = float(os.environ.get("PIPE_WRITE_TIMEOUT_SEC", "2"))
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
WEBRTC_NACK = os.environ.get("WEBRTC_NACK", "1") not in ("0", "false", "no", "")
WEBRTC_JITTER_MS = int(os.environ.get("WEBRTC_JITTER_MS", "300"))

# Audio (Opus -> AAC). The video path is untouched by audio: audio rides a
# separate pipe into the SAME ffmpeg and is fed continuously (silence when the
# camera sends nothing) so it can never starve/stall video. If a session fails
# to come up with audio, the bridge automatically retries WITHOUT audio, so
# audio can never take video down. Set ENABLE_AUDIO=0 to turn it off entirely.
ENABLE_AUDIO = os.environ.get("ENABLE_AUDIO", "1") not in ("0", "false", "no", "")
AUDIO_FALLBACK_AFTER = int(os.environ.get("AUDIO_FALLBACK_AFTER", "2"))  # failed reconnects before dropping audio
# The camera's mic is quiet; boost it (dB) with a limiter so loud bangs don't clip.
AUDIO_GAIN_DB = float(os.environ.get("AUDIO_GAIN_DB", "15"))
AUDIO_RATE = 48000
AUDIO_CHANNELS = 2
AUDIO_FRAME_SEC = 0.02
AUDIO_FRAME_BYTES = int(AUDIO_RATE * AUDIO_FRAME_SEC) * AUDIO_CHANNELS * 2  # s16le
AUDIO_MAX_BACKLOG_BYTES = AUDIO_FRAME_BYTES * 25  # ~0.5s; drop older audio beyond this


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


class GstSession:
    def __init__(self, device_id: str, cfg: dict, publish_url: str, read_url: str,
                 audio: bool = False):
        self._stop_ts = 0.0                # when stop() was first called (0 = running)
        self._ffmpeg_lock = threading.Lock()
        self._rtcp_fb_logged = False
        self.ffmpeg_exited = False
        self.video_drops = 0               # video AUs dropped because ffmpeg stopped reading
        self.audio_drops = 0
        self.audio = audio                 # request + publish audio for this session
        self.audio_active = False          # real camera audio is arriving
        self.audio_bytes = 0
        self._audio_buf = bytearray()
        self._audio_lock = threading.Lock()
        self._audio_w = None               # write end of ffmpeg's audio pipe
        self._audio_stop = threading.Event()
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
        self._audio_stop.set()
        if self.loop:
            self.loop.quit()
        # Bounded (pipes closed -> EOF, SIGTERM, SIGKILL after the grace); this
        # runs on the stop() helper thread, never on the aiohttp loop.
        self._kill_ffmpeg()

    def _kill_ffmpeg(self) -> None:
        """Close ffmpeg's input pipes and make sure the child is gone. Idempotent,
        safe from any thread, bounded to FFMPEG_KILL_GRACE_SEC + 2 s.

        Closing the pipes matters more than the signal: ffmpeg 5.1 blocked in
        read() on the video pipe does not act on SIGTERM, but EOF makes it
        finish. Closing the read side also unblocks an audio writer stuck in
        write() on the full audio pipe (EPIPE)."""
        with self._ffmpeg_lock:
            proc, self.ffmpeg = self.ffmpeg, None
            aw, self._audio_w = self._audio_w, None
        if aw is not None:
            try:
                aw.close()
            except Exception:
                pass
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

    def _write_pipe(self, fobj, data, timeout: float) -> bool:
        """Write `data` to a non-blocking pipe, waiting at most `timeout` seconds
        in total for ffmpeg to drain it. Returns False (data partly or wholly
        dropped) if ffmpeg stopped reading or is gone. Never blocks for good."""
        if fobj is None:
            return False
        try:
            fd = fobj.fileno()
        except Exception:
            return False
        view = memoryview(data)
        deadline = time.monotonic() + timeout
        while view:
            try:
                n = os.write(fd, view)
                view = view[n:]
                continue
            except BlockingIOError:
                pass
            except OSError:
                return False  # EPIPE / closed: ffmpeg is gone
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            try:
                _, ready, _ = select.select([], [fd], [], remaining)
            except (OSError, ValueError):
                return False
            if not ready:
                return False
        return True

    def _teardown(self):
        """Release the pipeline. Runs on the session thread only."""
        self._kill_ffmpeg()  # idempotent; covers a stop() that never ran
        pipe, self.pipe = self.pipe, None
        if pipe is None:
            return
        try:
            pipe.set_state(Gst.State.NULL)
        except Exception as err:  # noqa: BLE001
            _LOG.warning("[%s] pipeline teardown failed: %s", self.device_id, err)
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

            self.loop = GLib.MainLoop()
            if self._stopping.is_set():
                return  # stop() raced the handshake; don't enter the loop
            self.loop.run()
        except Exception as err:  # noqa: BLE001
            _LOG.exception("[%s] session crashed: %s", dev, err)

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
        _LOG.info("[%s] wss open (flushing %d queued)", self.device_id,
                  len(self._ws_queue))
        self._ws_ready = True
        for m in self._ws_queue:
            try:
                ws.send(m)
            except Exception as err:  # noqa: BLE001
                _LOG.warning("[%s] ws flush err: %s", self.device_id, err)
        self._ws_queue = []

    def _ws_send(self, action: str, payload: str):
        msg = json.dumps({
            "action": action,
            "messagePayload": base64.b64encode(payload.encode()).decode(),
        })
        if not self._ws_ready:
            self._ws_queue.append(msg)  # hold until the socket is open
            return
        try:
            self.ws.send(msg)
        except Exception as err:  # noqa: BLE001
            _LOG.warning("[%s] ws send err: %s", self.device_id, err)

    # ---- incoming media -> ffmpeg -> RTSP publish (no re-encode) ----
    def _on_pad(self, webrtc, pad):
        if pad.direction != Gst.PadDirection.SRC:
            return
        caps = pad.get_current_caps()
        name = caps.to_string() if caps else ""
        _LOG.info("[%s] pad-added: %s", self.device_id, name[:80])
        if "audio" in name:
            self._on_audio_pad(pad)
            return
        if "video" not in name:
            return

        # ffmpeg reads Annex-B H.264 from stdin, copies it to RTSP. With audio
        # enabled it ALSO reads raw PCM from a second pipe and encodes AAC; the
        # video arguments are identical to the video-only pipeline.
        if self.audio:
            ar, aw = os.pipe()
            cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning",
                   "-fflags", "+genpts", "-thread_queue_size", "512",
                   "-framerate", "30", "-f", "h264", "-i", "pipe:0",
                   "-thread_queue_size", "512",
                   "-f", "s16le", "-ar", str(AUDIO_RATE), "-ac", str(AUDIO_CHANNELS),
                   "-i", f"pipe:{ar}",
                   "-map", "0:v", "-map", "1:a",
                   "-c:v", "copy",
                   "-filter:a", f"volume={AUDIO_GAIN_DB}dB,alimiter=limit=0.9:level=disabled",
                   "-c:a", "aac", "-b:a", "64k",
                   "-f", "rtsp", "-rtsp_transport", "tcp", self.publish_url]
            self.ffmpeg = subprocess.Popen(cmd, stdin=subprocess.PIPE, pass_fds=(ar,))
            os.close(ar)  # parent keeps only the write end
            os.set_blocking(aw, False)  # writes are bounded by _write_pipe
            self._audio_w = os.fdopen(aw, "wb", buffering=0)
            threading.Thread(target=self._audio_writer, daemon=True).start()
        else:
            self.ffmpeg = subprocess.Popen(
                ["ffmpeg", "-hide_banner", "-loglevel", "warning",
                 "-fflags", "+genpts", "-f", "h264", "-i", "pipe:0",
                 "-c:v", "copy", "-f", "rtsp", "-rtsp_transport", "tcp",
                 self.publish_url],
                stdin=subprocess.PIPE,
            )
        os.set_blocking(self.ffmpeg.stdin.fileno(), False)
        # request-keyframe: a PLI goes back to the camera when the depayloader
        # sees RTP loss; wait-for-keyframe: the rest of that GOP is dropped
        # rather than forwarded as smeared frames (only meaningful with AU
        # alignment, hence the capsfilter right after the depayloader).
        chain = Gst.parse_bin_from_description(
            "rtph264depay request-keyframe=true wait-for-keyframe=true ! "
            "video/x-h264,alignment=au ! "
            "h264parse config-interval=1 ! "
            "video/x-h264,stream-format=byte-stream,alignment=au ! "
            "appsink name=out emit-signals=true sync=false max-buffers=20 drop=false",
            True,
        )
        self.pipe.add(chain)
        chain.sync_state_with_parent()
        pad.link(chain.get_static_pad("sink"))
        appsink = chain.get_by_name("out")
        appsink.connect("new-sample", self._on_sample)
        self.started = True
        _LOG.info("[%s] publishing via ffmpeg (audio=%s) -> %s",
                  self.device_id, self.audio, self.publish_url)

    # ---- audio: Opus -> raw PCM -> (paced, never-starving) -> ffmpeg ----
    def _on_audio_pad(self, pad):
        if not self.audio:
            return
        try:
            chain = Gst.parse_bin_from_description(
                "rtpopusdepay ! opusdec ! audioconvert ! audioresample ! "
                f"audio/x-raw,format=S16LE,rate={AUDIO_RATE},channels={AUDIO_CHANNELS},"
                "layout=interleaved ! "
                "appsink name=aout emit-signals=true sync=false max-buffers=50 drop=true",
                True,
            )
            self.pipe.add(chain)
            chain.sync_state_with_parent()
            pad.link(chain.get_static_pad("sink"))
            chain.get_by_name("aout").connect("new-sample", self._on_audio_sample)
            _LOG.info("[%s] audio branch linked (Opus -> PCM)", self.device_id)
        except Exception as err:  # noqa: BLE001
            # Audio failure must never affect video: just carry on with silence.
            _LOG.warning("[%s] audio branch failed (video unaffected): %s",
                         self.device_id, err)

    def _on_audio_sample(self, sink):
        sample = sink.emit("pull-sample")
        if sample:
            buf = sample.get_buffer()
            ok, mapinfo = buf.map(Gst.MapFlags.READ)
            if ok:
                with self._audio_lock:
                    self._audio_buf += bytes(mapinfo.data)
                    over = len(self._audio_buf) - AUDIO_MAX_BACKLOG_BYTES
                    if over > 0:
                        del self._audio_buf[:over]  # keep latency bounded
                self.audio_bytes += mapinfo.size
                self.audio_active = True
                buf.unmap(mapinfo)
        return Gst.FlowReturn.OK

    def _audio_writer(self):
        """Feed ffmpeg's audio pipe in real time, 20 ms at a time.

        Real camera audio is used when available, silence otherwise, so
        ffmpeg's audio input never starves (a starved input is what stalled
        video last time). Runs in its own thread: if it blocks, only audio
        waits -- the video callback is separate.
        """
        silence = bytes(AUDIO_FRAME_BYTES)
        nxt = time.monotonic()
        while not self._audio_stop.is_set():
            if not self.ffmpeg_alive():
                return  # ffmpeg gone / session stopping
            with self._audio_lock:
                if len(self._audio_buf) >= AUDIO_FRAME_BYTES:
                    chunk = bytes(self._audio_buf[:AUDIO_FRAME_BYTES])
                    del self._audio_buf[:AUDIO_FRAME_BYTES]
                else:
                    chunk = silence
            # Bounded: a full pipe means ffmpeg stopped reading (its video input
            # stalled, or it is dying). Drop the chunk; never block for good
            # here, which is what pinned 17 writer threads before 2026-11-12.
            if not self._write_pipe(self._audio_w, chunk, 0.5):
                self.audio_drops += 1
            nxt += AUDIO_FRAME_SEC
            delay = nxt - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            elif delay < -0.5:
                nxt = time.monotonic()  # fell behind; resync

    def _on_sample(self, sink):
        sample = sink.emit("pull-sample")
        proc = self.ffmpeg
        if sample and proc is not None and proc.stdin:
            buf = sample.get_buffer()
            ok, mapinfo = buf.map(Gst.MapFlags.READ)
            if ok:
                # Bounded write: if ffmpeg is not draining the pipe the AU is
                # dropped and the heartbeat is NOT refreshed, so the watchdog
                # sees the session as stale and restarts it, instead of this
                # GStreamer streaming thread blocking here for good.
                if self._write_pipe(proc.stdin, mapinfo.data, PIPE_WRITE_TIMEOUT_SEC):
                    self.last_sample_ts = time.time()  # heartbeat: video is flowing
                else:
                    self.video_drops += 1
                buf.unmap(mapinfo)
        return Gst.FlowReturn.OK


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


def _spawn_session(device_id, cfg, pub, read_url, attempts=0) -> GstSession:
    # Audio only on fresh/early sessions; after repeated failures fall back to
    # video-only so audio can never keep video from coming up.
    use_audio = ENABLE_AUDIO and attempts < AUDIO_FALLBACK_AFTER
    if ENABLE_AUDIO and not use_audio:
        _LOG.info("[%s] audio disabled for this attempt (fallback to video-only)",
                  device_id)
    sess = GstSession(device_id, cfg, pub, read_url, audio=use_audio)
    sess.reconnect_attempts = attempts
    SESSIONS[device_id] = sess
    sess.start()
    return sess


async def handle_start(request: web.Request) -> web.Response:
    device_id = request.match_info["device_id"]
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"error": "expected JSON"}, status=400)
    safe = device_id.replace("/", "_")
    read_url = f"{RTSP_READ_BASE.rstrip('/')}/telus_{safe}"
    pub = f"{RTSP_PUBLISH_BASE.rstrip('/')}/telus_{safe}"

    # Reuse ONLY a session that is actually healthy (video flowing) or still in
    # its initial handshake. A session whose thread is "alive" but has gone
    # stale (camera dropped it) is NOT reused -- we restart it below with the
    # fresh credentials HA just sent. This is what fixes "works sometimes" and
    # "have to reload every ~10 min".
    with SESSIONS_LOCK:
        existing = SESSIONS.get(device_id)
        if (
            existing
            and existing._thread.is_alive()
            and (existing.is_healthy() or existing.is_connecting())
        ):
            return web.json_response({"rtsp_url": read_url, "reused": True})
        old = SESSIONS.pop(device_id, None)
    if old:
        # Off the event loop: a blocking stop here is what froze /health and
        # /start for 27 h on 2026-11-04.
        await asyncio.get_running_loop().run_in_executor(None, _stop_session, old)

    cfg = parse_live_config(payload)
    with SESSIONS_LOCK:
        sess = _spawn_session(device_id, cfg, pub, read_url, attempts=0)
    # give it a moment to connect/publish (without blocking other requests)
    for _ in range(60):
        if sess.started:
            break
        await asyncio.sleep(0.2)
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
    while True:
        time.sleep(WATCHDOG_INTERVAL_SEC)
        try:
            _reap_stopped_sessions()
            with SESSIONS_LOCK:
                items = list(SESSIONS.items())
            now = time.time()
            for dev, sess in items:
                if not sess._thread.is_alive():
                    continue
                if sess.is_healthy():
                    sess.reconnect_attempts = 0  # flowing again -> reset counter
                    continue
                if sess.is_connecting():
                    continue  # still in handshake grace
                # started once, now no frames past the stale window -> dead
                if not sess.started or (now - sess.last_sample_ts) <= STREAM_STALE_SEC:
                    continue
                if sess.reconnect_attempts >= MAX_AUTO_RECONNECTS:
                    continue  # give up until a fresh /start resets us
                attempts = sess.reconnect_attempts + 1
                _LOG.info("[%s] stale (no frames %.1fs) -> auto-reconnect %d/%d",
                          dev, now - sess.last_sample_ts, attempts, MAX_AUTO_RECONNECTS)
                cfg, pub, read_url = sess.cfg, sess.publish_url, sess.read_url
                _stop_session(sess)
                with SESSIONS_LOCK:
                    if SESSIONS.get(dev) is sess:  # not replaced by HA meanwhile
                        _spawn_session(dev, cfg, pub, read_url, attempts=attempts)
        except Exception as err:  # noqa: BLE001
            _LOG.warning("watchdog error: %s", err)


async def handle_stop(request: web.Request) -> web.Response:
    device_id = request.match_info["device_id"]
    sess = SESSIONS.pop(device_id, None)
    if sess:
        await asyncio.get_running_loop().run_in_executor(None, _stop_session, sess)
    return web.json_response({"stopped": bool(sess)})


async def handle_health(request: web.Request) -> web.Response:
    sessions = {
        dev: {
            "healthy": s.is_healthy(),
            "started": s.started,
            "secs_since_frame": round(time.time() - s.last_sample_ts, 1)
            if s.last_sample_ts else None,
            "reconnect_attempts": s.reconnect_attempts,
            "audio_requested": s.audio,
            "audio_active": s.audio_active,
            "audio_bytes": s.audio_bytes,
            "video_drops": s.video_drops,
            "audio_drops": s.audio_drops,
        }
        for dev, s in list(SESSIONS.items())
    }
    return web.json_response({"ok": True, "sessions": sessions,
                              "process": _process_metrics()})


def make_app() -> web.Application:
    app = web.Application()
    app.router.add_post("/session/{device_id}/start", handle_start)
    app.router.add_post("/session/{device_id}/stop", handle_stop)
    app.router.add_get("/health", handle_health)
    return app


if __name__ == "__main__":
    _LOG.info("TELUS GStreamer bridge on :%d  publish=%s read=%s",
              LISTEN_PORT, RTSP_PUBLISH_BASE, RTSP_READ_BASE)
    threading.Thread(target=_watchdog, daemon=True).start()
    _LOG.info("watchdog started (stale>%ss, grace=%ss, max_reconnects=%d, stop_timeout=%ss)",
              STREAM_STALE_SEC, STARTUP_GRACE_SEC, MAX_AUTO_RECONNECTS, STOP_TIMEOUT_SEC)
    # `docker kill -s USR1 <bridge container>` dumps every thread's stack to
    # stderr (docker logs), so a future hang can be located instead of guessed.
    faulthandler.register(signal.SIGUSR1, all_threads=True)
    web.run_app(make_app(), host="0.0.0.0", port=LISTEN_PORT)

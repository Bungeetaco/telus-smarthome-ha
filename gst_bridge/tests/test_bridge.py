"""Bridge unit tests. Run INSIDE the bridge image (GStreamer + ffmpeg present):

    cd gst_bridge && docker compose build && \
    docker run --rm --init --ulimit nofile=65536:65536 -v "$PWD/tests:/t:ro" \
        gst_bridge-bridge python3 -I -m unittest discover -s /t -v

Stdlib unittest only: the image has no pytest.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, "/app")
os.environ.setdefault("FFMPEG_KILL_GRACE_SEC", "2")
import telus_gst_bridge as b  # noqa: E402

from gi.repository import Gst  # noqa: E402


def mk(audio=True):
    return b.GstSession("dev", {}, "rtsp://127.0.0.1:1/x", "rtsp://127.0.0.1:1/x", audio=audio)


def ffprobe_packets(path, stream):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", stream, "-show_entries",
         "packet=pts_time", "-of", "json", path], capture_output=True, text=True, check=True).stdout
    pts = [float(p["pts_time"]) for p in json.loads(out)["packets"] if p.get("pts_time") not in (None, "N/A")]
    return pts


class KillFfmpeg(unittest.TestCase):
    def test_child_blocked_in_read_is_gone(self):
        s = mk()
        s.ffmpeg = subprocess.Popen(["sleep", "60"], stdin=subprocess.PIPE)
        t0 = time.monotonic(); s._kill_ffmpeg(); dt = time.monotonic() - t0
        self.assertIsNone(s.ffmpeg)
        self.assertLess(dt, b.FFMPEG_KILL_GRACE_SEC + 3)
        self.assertFalse(s.ffmpeg_alive())

    def test_child_ignoring_sigterm_is_killed(self):
        s = mk()
        s.ffmpeg = subprocess.Popen(["sh", "-c", 'trap "" TERM; sleep 60'], stdin=subprocess.PIPE)
        pid = s.ffmpeg.pid
        time.sleep(0.2)
        t0 = time.monotonic(); s._kill_ffmpeg(); dt = time.monotonic() - t0
        self.assertGreaterEqual(dt, b.FFMPEG_KILL_GRACE_SEC - 0.1)
        self.assertLess(dt, b.FFMPEG_KILL_GRACE_SEC + 3)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_real_ffmpeg_blocked_on_empty_stdin_and_stop(self):
        """Production argv, nothing ever written: stop() must take it down."""
        s = mk()
        cmd = s._ffmpeg_cmd(audio=True)
        self.assertEqual(cmd[-5:], ["-f", "rtsp", "-rtsp_transport", "tcp", s.publish_url])
        cmd = cmd[:-5] + ["-f", "null", "-"]
        s.ffmpeg = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
        time.sleep(1.5)
        self.assertIsNone(s.ffmpeg.poll(), "ffmpeg should be waiting on stdin")
        t0 = time.monotonic(); s.stop(); dt = time.monotonic() - t0
        self.assertIsNone(s.ffmpeg)
        self.assertLess(dt, b.FFMPEG_KILL_GRACE_SEC + 4)

    def test_reaper_kills_stopped_sessions_child(self):
        s = mk()
        s.ffmpeg = subprocess.Popen(["sleep", "60"], stdin=subprocess.PIPE)
        s._stop_ts = time.time() - 100
        b._reap_stopped_sessions()
        self.assertIsNone(s.ffmpeg)
        self.assertEqual(b._process_metrics()["ffmpeg_children"], 0)

    def test_grow_pipe(self):
        import fcntl
        r, w = os.pipe()
        b._grow_pipe(w)
        self.assertGreaterEqual(fcntl.fcntl(w, fcntl.F_GETPIPE_SZ), b.PIPE_BUFFER_BYTES)
        os.close(r); os.close(w)


class MediaChain(unittest.TestCase):
    """The exact production chain (VIDEO_BRANCH + AUDIO_BRANCH + MUX_CHAIN) fed by a
    synthetic 30 fps H.264 RTP source and a 48 kHz Opus RTP source, written to a
    file through fdsink exactly as in production. The timestamp contract that
    was broken before 2026-11-12 (fixed 19 fps from the SPS VUI) is asserted on
    the Matroska output AND on ffmpeg's remux of it."""

    FPS = 30
    SECONDS = 3

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.mkv = os.path.join(cls.tmp, "chain.mkv")
        fd = os.open(cls.mkv, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        desc = (
            f"videotestsrc num-buffers={cls.FPS * cls.SECONDS} is-live=false ! "
            f"video/x-raw,width=320,height=240,framerate={cls.FPS}/1 ! "
            "x264enc tune=zerolatency key-int-max=30 speed-preset=ultrafast ! "
            "rtph264pay pt=101 config-interval=1 ! "
            "application/x-rtp,media=video,encoding-name=H264,payload=101,clock-rate=90000 ! "
            + b.VIDEO_BRANCH + " "
            f"audiotestsrc num-buffers={50 * cls.SECONDS} samplesperbuffer=960 is-live=false ! "
            "audio/x-raw,rate=48000,channels=2 ! opusenc ! rtpopuspay pt=111 ! "
            "application/x-rtp,media=audio,encoding-name=OPUS,payload=111,clock-rate=48000 ! "
            + b.AUDIO_BRANCH + " "
            + b.MUX_CHAIN % fd
        )
        pipe = Gst.parse_launch(desc)
        pipe.set_state(Gst.State.PLAYING)
        msg = pipe.get_bus().timed_pop_filtered(
            30 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
        pipe.set_state(Gst.State.NULL)
        os.close(fd)
        if msg is None or msg.type == Gst.MessageType.ERROR:
            raise RuntimeError(f"pipeline failed: {msg.parse_error() if msg else 'timeout'}")

    def test_mkv_has_both_tracks_with_real_timestamps(self):
        v = ffprobe_packets(self.mkv, "v:0")
        a = ffprobe_packets(self.mkv, "a:0")
        self.assertGreaterEqual(len(v), self.FPS * self.SECONDS - 5, "video frames missing")
        self.assertGreater(len(a), 100, "audio packets missing")
        deltas = [y - x for x, y in zip(v, v[1:])]
        self.assertTrue(all(d > 0 for d in deltas), "video PTS not strictly increasing")
        bad = [d for d in deltas if abs(d - 1 / self.FPS) > 0.004]
        self.assertLess(len(bad), 3, f"video PTS spacing is not 1/{self.FPS}: {bad[:5]}")
        # A/V start within 100 ms of each other (same clock, not a paced thread).
        self.assertLess(abs(v[0] - a[0]), 0.1, (v[0], a[0]))

    def test_ffmpeg_preserves_timestamps_from_mkv(self):
        """Production argv with the RTSP output swapped for a Matroska file."""
        s = mk()
        cmd = s._ffmpeg_cmd(audio=True)
        out = os.path.join(self.tmp, "remux.mkv")
        cmd = cmd[:-5] + ["-y", "-f", "matroska", out]
        with open(self.mkv, "rb") as fh:
            res = subprocess.run(cmd, stdin=fh, capture_output=True, timeout=60)
        self.assertEqual(res.returncode, 0, res.stderr.decode(errors="replace")[-500:])
        v = ffprobe_packets(out, "v:0")
        a = ffprobe_packets(out, "a:0")
        self.assertGreaterEqual(len(v), self.FPS * self.SECONDS - 5)
        self.assertGreater(len(a), 50, "AAC audio missing after remux")
        deltas = [y - x for x, y in zip(v, v[1:])]
        bad = [d for d in deltas if abs(d - 1 / self.FPS) > 0.004]
        self.assertLess(len(bad), 3, f"ffmpeg did not keep 1/{self.FPS} spacing: {bad[:5]}")
        # 3 s of input must be ~3 s of output (not 3*30/19 as with the VUI clock).
        self.assertAlmostEqual(v[-1] - v[0], (len(v) - 1) / self.FPS, delta=0.1)

    def test_build_chain_strings(self):
        self.assertIn("fd=7", b.build_chain(7, False))
        self.assertNotIn("rtpopusdepay", b.build_chain(7, False))
        self.assertIn("mux.audio_0", b.build_chain(7, True))


class Pll(unittest.TestCase):
    """Bursty arrivals (as measured on the wire) in, smooth monotonic clock out."""

    def _run(self, fps, burst, pause_ms, seconds, jitter_ms=0):
        import random
        rnd = random.Random(1)
        pll = b._Pll(33_000_000, window=60)
        T = 1e9 / fps
        out, arr = [], []
        t = 0.0
        n = 0
        while t < seconds * 1e9:
            # `burst` frames captured one interval apart are delivered together
            base = t + pause_ms * 1e6
            for i in range(burst):
                a = int(base + rnd.uniform(0, jitter_ms) * 1e6)
                arr.append(a); out.append(pll.update(a)); n += 1
            t += burst * T
        return arr, out, T

    def test_smooth_and_tracking(self):
        arr, out, T = self._run(fps=27, burst=5, pause_ms=150, seconds=20, jitter_ms=2)
        d = [y - x for x, y in zip(out, out[1:])]
        self.assertTrue(all(x > 0 for x in d), "not monotonic")
        steady = d[200:]  # after lock-in
        bad = [x for x in steady if abs(x - T) > 0.35 * T]
        self.assertLess(len(bad) / len(steady), 0.05, f"spacing off in {len(bad)}/{len(steady)}: {bad[:5]}")
        # long-term: output clock tracks real time within the burst length
        self.assertLess(abs(out[-1] - arr[-1]), 0.4e9, (out[-1] - arr[-1]) / 1e9)
        self.assertAlmostEqual((out[-1] - out[200]) / (len(out) - 201), T, delta=0.03 * T)

    def test_rate_change_and_stall(self):
        pll = b._Pll(33_000_000, window=60)
        t = 0; outs = []
        for _ in range(300):              # 30 fps, smooth
            t += 33_333_333; outs.append(pll.update(t))
        t += 2_000_000_000               # 2 s stall, then a burst of the backlog
        for _ in range(60):
            t += 1000; outs.append(pll.update(t))
        for _ in range(600):             # then 15 fps
            t += 66_666_667; outs.append(pll.update(t))
        d = [y - x for x, y in zip(outs, outs[1:])]
        self.assertTrue(all(x > 0 for x in d))
        self.assertLess(abs(outs[-1] - t), 0.5e9, "did not re-lock after the stall")
        tail = d[-100:]
        self.assertTrue(all(abs(x - 66_666_667) < 0.35 * 66_666_667 for x in tail), tail[:5])


class LiveConfig(unittest.TestCase):
    PAYLOAD = {"liveConfig": {"data": {
        "channelArn": "arn:aws:kinesisvideo:ca-central-1:1:channel/x/1",
        "client": {"credentials": {"AccessKeyId": "AKIA", "SecretAccessKey": "s", "SessionToken": "t"},
                   "resourceEndpointList": [{"Protocol": "WSS", "ResourceEndpoint": "wss://m.kinesisvideo.ca-central-1.amazonaws.com"},
                                            {"Protocol": "HTTPS", "ResourceEndpoint": "https://r.kinesisvideo.ca-central-1.amazonaws.com"}]}}}}

    def test_parse_nested_data(self):
        c = b.parse_live_config(self.PAYLOAD)
        self.assertEqual(c["region"], "ca-central-1")
        self.assertEqual(c["wss_endpoint"], "wss://m.kinesisvideo.ca-central-1.amazonaws.com")

    def test_parse_rejects_garbage(self):
        with self.assertRaises((KeyError, AttributeError, TypeError)):
            b.parse_live_config({"liveConfig": {"nope": 1}})

    def test_signed_url(self):
        url = b.sign_wss_url(b.parse_live_config(self.PAYLOAD), "ha-test")
        for needle in ("X-Amz-ChannelARN", "X-Amz-ClientId=ha-test", "X-Amz-Expires=299", "X-Amz-Signature"):
            self.assertIn(needle, url)

    def test_device_id_regex(self):
        self.assertTrue(b.DEVICE_ID_RE.fullmatch("db-C0F5358935AC"))
        for bad in ("a/b", "a b", "x%0d%0a", "a?b", "", "x" * 65):
            self.assertIsNone(b.DEVICE_ID_RE.fullmatch(bad), bad)


if __name__ == "__main__":
    unittest.main()

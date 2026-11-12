"""Bridge unit tests. Run INSIDE the bridge image (GStreamer + ffmpeg present):

    cd gst_bridge && docker compose build && \
    docker run --rm --init -v "$PWD/tests:/t:ro" gst_bridge-bridge \
        python3 -I -m unittest discover -s /t -v

Stdlib unittest only: the image has no pytest.
"""
import os
import subprocess
import sys
import threading
import time
import unittest

sys.path.insert(0, "/app")
os.environ.setdefault("FFMPEG_KILL_GRACE_SEC", "2")
import telus_gst_bridge as b  # noqa: E402

from gi.repository import Gst  # noqa: E402


def mk(audio=True):
    return b.GstSession("dev", {}, "rtsp://127.0.0.1:1/x", "rtsp://127.0.0.1:1/x", audio=audio)


class WritePipe(unittest.TestCase):
    def test_stalled_reader_drops_within_timeout(self):
        s = mk()
        sleeper = subprocess.Popen(["sleep", "60"], stdin=subprocess.PIPE)
        try:
            os.set_blocking(sleeper.stdin.fileno(), False)
            t0 = time.monotonic()
            ok = s._write_pipe(sleeper.stdin, b"x" * (1 << 20), 0.5)
            dt = time.monotonic() - t0
            self.assertFalse(ok)
            self.assertLess(dt, 1.5)
            self.assertGreater(dt, 0.4)
        finally:
            sleeper.kill(); sleeper.wait()

    def test_fd_above_1024_still_works(self):
        """select() would raise ValueError here; poll() must not."""
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        hi = 1500
        if soft <= hi:
            if hard <= hi and hard != resource.RLIM_INFINITY:
                self.skipTest(f"RLIMIT_NOFILE hard limit {hard} too low for fd {hi}")
            resource.setrlimit(resource.RLIMIT_NOFILE, (hi + 100, hard))
        r, w = os.pipe()
        os.dup2(w, hi); os.close(w)
        os.set_blocking(hi, False)
        s = mk()

        class F:  # minimal file-like
            def fileno(self):
                return hi
        got = bytearray()

        def reader():
            while len(got) < 300_000:
                got.extend(os.read(r, 65536))
        t = threading.Thread(target=reader, daemon=True); t.start()
        self.assertTrue(s._write_pipe(F(), b"y" * 300_000, 2.0))
        t.join(2)
        self.assertEqual(len(got), 300_000)
        os.close(hi); os.close(r)

    def test_closed_reader_is_false_immediately(self):
        r, w = os.pipe(); os.close(r)
        os.set_blocking(w, False)

        class F:
            def fileno(self):
                return w
        t0 = time.monotonic()
        self.assertFalse(mk()._write_pipe(F(), b"z" * 10, 1.0))
        self.assertLess(time.monotonic() - t0, 0.2)
        os.close(w)

    def test_grow_pipe(self):
        r, w = os.pipe()
        b._grow_pipe(w)
        import fcntl
        self.assertGreaterEqual(fcntl.fcntl(w, fcntl.F_GETPIPE_SZ), b.PIPE_BUFFER_BYTES)
        os.close(r); os.close(w)


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

    def test_real_ffmpeg_with_full_audio_pipe_and_stop(self):
        """Production argv, video pipe starved: the audio writer must keep
        running (dropping) and stop() must take everything down."""
        s = mk()
        ar, aw = os.pipe()
        cmd = s._ffmpeg_cmd(audio_fd=ar)
        self.assertEqual(cmd[-5:], ["-f", "rtsp", "-rtsp_transport", "tcp", s.publish_url])
        cmd = cmd[:-5] + ["-f", "null", "-"]
        s.ffmpeg = subprocess.Popen(cmd, stdin=subprocess.PIPE, pass_fds=(ar,),
                                    stderr=subprocess.DEVNULL)
        os.close(ar)
        os.set_blocking(aw, False); s._audio_w = os.fdopen(aw, "wb", buffering=0)
        os.set_blocking(s.ffmpeg.stdin.fileno(), False)
        th = threading.Thread(target=s._audio_writer, daemon=True); th.start()
        time.sleep(3)
        self.assertTrue(th.is_alive())
        self.assertGreater(s.audio_drops, 0)
        s.stop(); th.join(3)
        self.assertIsNone(s.ffmpeg)
        self.assertFalse(th.is_alive())

    def test_reaper_kills_stopped_sessions_child(self):
        s = mk()
        s.ffmpeg = subprocess.Popen(["sleep", "60"], stdin=subprocess.PIPE)
        s._stop_ts = time.time() - 100
        b._reap_stopped_sessions()
        self.assertIsNone(s.ffmpeg)
        m = b._process_metrics()
        self.assertEqual(m["ffmpeg_children"], 0)


class NeedIdr(unittest.TestCase):
    """After a dropped write, delta frames are skipped until a keyframe."""

    class FakeSink:
        def __init__(self, buf):
            self.buf = buf

        def emit(self, _):
            class S:
                def __init__(self, buf):
                    self._b = buf

                def get_buffer(self):
                    return self._b
            return S(self.buf)

    def _buf(self, delta):
        buf = Gst.Buffer.new_wrapped(b"\x00\x00\x00\x01\x65" + b"\x00" * 100)
        if delta:
            buf.set_flags(Gst.BufferFlags.DELTA_UNIT)
        return buf

    def test_skip_until_keyframe(self):
        s = mk(audio=False)
        s.ffmpeg = subprocess.Popen(["cat"], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL)
        os.set_blocking(s.ffmpeg.stdin.fileno(), False)
        try:
            s._need_idr = True
            s._on_sample(self.FakeSink(self._buf(delta=True)))
            self.assertEqual(s.video_drops, 1)
            self.assertTrue(s._need_idr)
            s._on_sample(self.FakeSink(self._buf(delta=False)))
            self.assertFalse(s._need_idr)
            self.assertEqual(s.video_drops, 1)
            self.assertGreater(s.last_sample_ts, 0)
        finally:
            s._kill_ffmpeg()

    def test_failed_write_sets_need_idr(self):
        s = mk(audio=False)
        s.ffmpeg = subprocess.Popen(["sleep", "60"], stdin=subprocess.PIPE)
        os.set_blocking(s.ffmpeg.stdin.fileno(), False)
        try:
            os.write(s.ffmpeg.stdin.fileno(), b"\0" * 65536)  # fill the 64K pipe
        except BlockingIOError:
            pass
        old = b.PIPE_WRITE_TIMEOUT_SEC; b.PIPE_WRITE_TIMEOUT_SEC = 0.2
        try:
            buf = Gst.Buffer.new_wrapped(b"\x00" * 200_000)
            s._on_sample(self.FakeSink(buf))
            self.assertTrue(s._need_idr)
            self.assertEqual(s.video_drops, 1)
        finally:
            b.PIPE_WRITE_TIMEOUT_SEC = old
            s._kill_ffmpeg()


class VideoChain(unittest.TestCase):
    def test_production_chain_outputs_monotonic_au(self):
        """The exact VIDEO_CHAIN against a synthetic 30 fps RTP source: AU
        aligned, strictly increasing PTS, ~1/30 s spacing."""
        desc = ("videotestsrc num-buffers=60 ! video/x-raw,width=320,height=240,framerate=30/1 ! "
                "x264enc tune=zerolatency key-int-max=30 speed-preset=ultrafast ! "
                "rtph264pay pt=101 ! application/x-rtp,media=video,encoding-name=H264,"
                "payload=101,clock-rate=90000 ! " + b.VIDEO_CHAIN)
        pipe = Gst.parse_launch(desc)
        sink = pipe.get_by_name("out")
        pts = []

        def on_sample(s):
            smp = s.emit("pull-sample")
            pts.append(smp.get_buffer().pts)
            return Gst.FlowReturn.OK
        sink.connect("new-sample", on_sample)
        pipe.set_state(Gst.State.PLAYING)
        bus = pipe.get_bus()
        msg = bus.timed_pop_filtered(15 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
        pipe.set_state(Gst.State.NULL)
        self.assertIsNotNone(msg)
        self.assertEqual(msg.type, Gst.MessageType.EOS, msg.parse_error() if msg.type == Gst.MessageType.ERROR else "")
        self.assertGreaterEqual(len(pts), 50)
        deltas = [b2 - a for a, b2 in zip(pts, pts[1:])]
        self.assertTrue(all(d > 0 for d in deltas), "PTS not strictly increasing")
        self.assertTrue(all(abs(d - Gst.SECOND / 30) < 2 * Gst.MSECOND for d in deltas), deltas[:5])


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

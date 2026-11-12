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
    the MPEG-TS output AND on ffmpeg's remux of it."""

    FPS = 30
    SECONDS = 3

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.mkv = os.path.join(cls.tmp, "chain.ts")
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
            + b.AUDIO_BRANCH + " " + b.AUDIO_MIX + " "
            + b.MUX_CHAIN % fd
        )
        pipe = Gst.parse_launch(desc)
        pipe.set_state(Gst.State.PLAYING)
        # The silence source is live and endless (as in production), so the
        # pipeline is ended from here once the finite sources have played out.
        time.sleep(cls.SECONDS + 1.5)
        pipe.send_event(Gst.Event.new_eos())
        msg = pipe.get_bus().timed_pop_filtered(
            30 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
        pipe.set_state(Gst.State.NULL)
        os.close(fd)
        if msg is None or msg.type == Gst.MessageType.ERROR:
            raise RuntimeError(f"pipeline failed: {msg.parse_error() if msg else 'timeout'}")

    def test_ts_has_both_tracks_with_real_timestamps(self):
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

    def test_ffmpeg_preserves_timestamps_from_ts(self):
        """Production argv with the RTSP output swapped for a Matroska file (any muxer that keeps PTS)."""
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


    def test_mid_stream_resolution_change_survives(self):
        """The camera re-sends a different SPS on day/night or bitrate switches.
        Matroska died on it ("Could not multiplex stream", 2026-11-12); MPEG-TS
        must carry the new parameters in-band. Two live sources at different
        sizes feed the production video branch through an input-selector that
        is switched after ~1.5 s; the output must contain both sizes and the
        bus must not report an error."""
        out = os.path.join(self.tmp, "switch.ts")
        fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        desc = (
            "input-selector name=sel ! x264enc tune=zerolatency key-int-max=15 speed-preset=ultrafast ! "
            "rtph264pay pt=101 config-interval=1 ! "
            "application/x-rtp,media=video,encoding-name=H264,payload=101,clock-rate=90000 ! "
            + b.VIDEO_BRANCH + " "
            "videotestsrc is-live=true num-buffers=60 pattern=smpte ! video/x-raw,width=320,height=240,framerate=30/1 ! sel.sink_0 "
            "videotestsrc is-live=true num-buffers=120 pattern=ball ! video/x-raw,width=640,height=480,framerate=30/1 ! sel.sink_1 "
            + b.MUX_CHAIN % fd
        )
        pipe = Gst.parse_launch(desc)
        sel = pipe.get_by_name("sel")
        pipe.set_state(Gst.State.PLAYING)
        time.sleep(1.5)
        sel.set_property("active-pad", sel.get_static_pad("sink_1"))
        msg = pipe.get_bus().timed_pop_filtered(
            20 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
        pipe.set_state(Gst.State.NULL)
        os.close(fd)
        self.assertIsNotNone(msg, "pipeline neither finished nor errored in 20 s")
        self.assertNotEqual(msg.type, Gst.MessageType.ERROR,
                            msg.parse_error() if msg.type == Gst.MessageType.ERROR else "")
        widths = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "frame=width",
             "-of", "csv=p=0", out], capture_output=True, text=True, check=True).stdout.split()
        self.assertGreater(len(widths), 60, f"too few frames decoded: {len(widths)}")
        self.assertIn("320", widths[:30], "first frames should be 320 wide")
        self.assertIn("640", widths[-30:], "last frames should be 640 wide")


    def test_heartbeat_probe_sees_mux_output(self):
        """mpegtsmux emits buffer lists, not buffers. The watchdog heartbeat is
        a pad probe on fdsink; with a BUFFER-only probe it never fired and every
        session was torn down as stale after 8 s (live, 2026-11-12). The probe
        type the bridge uses must see the output; a BUFFER-only one is the trap."""
        seen = {"prod": 0, "buffer_only": 0}
        fd = os.open(os.devnull, os.O_WRONLY)
        pipe = Gst.parse_launch(
            "videotestsrc num-buffers=30 ! x264enc tune=zerolatency key-int-max=15 speed-preset=ultrafast ! "
            "rtph264pay pt=101 config-interval=1 ! "
            "application/x-rtp,media=video,encoding-name=H264,payload=101,clock-rate=90000 ! "
            + b.VIDEO_BRANCH + " " + b.MUX_CHAIN % fd)
        sink = pipe.get_by_name("out").get_static_pad("sink")

        def prod(pad, info):
            seen["prod"] += 1
            return Gst.PadProbeReturn.OK

        def buffer_only(pad, info):
            seen["buffer_only"] += 1
            return Gst.PadProbeReturn.OK

        sink.add_probe(b.OUT_PROBE_TYPE, prod)
        sink.add_probe(Gst.PadProbeType.BUFFER, buffer_only)
        pipe.set_state(Gst.State.PLAYING)
        msg = pipe.get_bus().timed_pop_filtered(
            10 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
        pipe.set_state(Gst.State.NULL)
        os.close(fd)
        self.assertEqual(msg.type, Gst.MessageType.EOS)
        self.assertGreater(seen["prod"], 20, seen)
        self.assertEqual(seen["buffer_only"], 0,
                         "mpegtsmux now pushes plain buffers; fine, but re-check the probe comment")


    def test_video_gate_drops_until_keyframe_and_requests_one(self):
        """A session that joins mid-GOP must not emit delta frames before the
        first IDR, and must ask upstream for a keyframe (GstForceKeyUnit, which
        rtpsession turns into a PLI). Emulated by dropping the encoder's first
        five frames so the depayloader starts on P-frames."""
        sess = b.GstSession("test", {}, "rtsp://x/pub", "rtsp://x/read", audio=False)
        pipe = Gst.parse_launch(
            "videotestsrc num-buffers=40 ! video/x-raw,width=160,height=120,framerate=30/1 ! "
            "x264enc name=enc tune=zerolatency key-int-max=30 speed-preset=ultrafast ! "
            "rtph264pay pt=101 config-interval=-1 ! "
            "application/x-rtp,media=video,encoding-name=H264,payload=101,clock-rate=90000 ! "
            # the depayloader's own gate is OFF here so the session's gate is
            # what is under test (in production both are on)
            "rtph264depay name=vdepay request-keyframe=false wait-for-keyframe=false ! "
            "video/x-h264,alignment=au ! fakesink name=sink")
        enc_src = pipe.get_by_name("enc").get_static_pad("src")
        state = {"n": 0, "kf_requests": 0, "passed": []}

        def drop_first(pad, info):
            state["n"] += 1
            return Gst.PadProbeReturn.DROP if state["n"] <= 5 else Gst.PadProbeReturn.OK

        def see_event(pad, info):
            ev = info.get_event()
            if ev is not None and ev.get_structure() is not None and \
                    ev.get_structure().get_name() == "GstForceKeyUnit":
                state["kf_requests"] += 1
            return Gst.PadProbeReturn.OK

        enc_src.add_probe(Gst.PadProbeType.BUFFER, drop_first)
        enc_src.add_probe(Gst.PadProbeType.EVENT_UPSTREAM, see_event)
        depay_src = pipe.get_by_name("vdepay").get_static_pad("src")
        depay_src.add_probe(Gst.PadProbeType.BUFFER, sess._on_au, "video")

        def record(pad, info):   # added after _on_au, so it sees what passed
            state["passed"].append(info.get_buffer().has_flags(Gst.BufferFlags.DELTA_UNIT))
            return Gst.PadProbeReturn.OK
        depay_src.add_probe(Gst.PadProbeType.BUFFER, record)
        pipe.set_state(Gst.State.PLAYING)
        msg = pipe.get_bus().timed_pop_filtered(
            10 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
        pipe.set_state(Gst.State.NULL)
        self.assertEqual(msg.type, Gst.MessageType.EOS)
        self.assertGreater(sess._pre_kf_dropped, 0, "nothing was dropped: did the stream start on a keyframe?")
        self.assertTrue(state["passed"], "no frame passed the gate")
        self.assertFalse(state["passed"][0], "the first frame that passed must be a keyframe")
        self.assertGreaterEqual(state["kf_requests"], 1,
                                f"no GstForceKeyUnit reached the encoder: {state} dropped={sess._pre_kf_dropped}")
        self.assertTrue(sess._kf_seen)
        # the encoder honoured the request well before its 30-frame interval
        self.assertLess(sess._pre_kf_dropped, 25, sess._pre_kf_dropped)


    def test_stream_opens_with_sps_pps_even_without_sprop(self):
        """The first video access unit in the TS must be SPS, PPS, IDR even when
        the SDP has no sprop-parameter-sets and the first RTP frames are not a
        keyframe (so the depayloader has to learn the parameter sets in-band
        and h264parse has to re-insert them)."""
        out = os.path.join(self.tmp, "open.ts")
        fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        pipe = Gst.parse_launch(
            "videotestsrc num-buffers=50 ! video/x-raw,width=160,height=120,framerate=25/1 ! "
            "x264enc name=enc tune=zerolatency key-int-max=25 speed-preset=ultrafast ! "
            # config-interval=0: parameter sets only where the encoder put them
            "rtph264pay pt=101 config-interval=0 ! "
            # strip sprop-parameter-sets (and everything else optional) from the caps
            "capssetter replace=true caps=\"application/x-rtp,media=video,encoding-name=H264,"
            "payload=101,clock-rate=90000\" ! "
            + b.VIDEO_BRANCH + " " + b.MUX_CHAIN % fd)
        n = {"i": 0}

        def drop_first_gop_head(pad, info):   # start the stream on P-frames
            n["i"] += 1
            return Gst.PadProbeReturn.DROP if n["i"] <= 3 else Gst.PadProbeReturn.OK
        pipe.get_by_name("enc").get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, drop_first_gop_head)
        pipe.set_state(Gst.State.PLAYING)
        msg = pipe.get_bus().timed_pop_filtered(
            15 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
        pipe.set_state(Gst.State.NULL)
        os.close(fd)
        self.assertEqual(msg.type, Gst.MessageType.EOS,
                         msg.parse_error() if msg.type == Gst.MessageType.ERROR else "")
        trace = subprocess.run(
            ["ffmpeg", "-v", "info", "-i", out, "-c:v", "copy", "-bsf:v", "trace_headers", "-f", "null", "-"],
            capture_output=True, text=True).stderr
        import re
        # trace_headers prints the field in binary ("nal_unit_type  00111 = 7")
        nals = [int(x, 2) for x in re.findall(r"nal_unit_type\s+([01]+)", trace)[:12]]
        self.assertTrue(nals, "trace_headers printed no NAL units")
        head = [x for x in nals if x not in (6, 9)]      # ignore SEI and AUD
        first_slice = next((i for i, x in enumerate(head) if x in (1, 5)), None)
        self.assertIsNotNone(first_slice, f"no slice in the first NALs: {nals}")
        self.assertEqual(head[first_slice], 5, f"first slice is not an IDR: {nals}")
        self.assertIn(7, head[:first_slice], f"no SPS before the first IDR: {nals}")
        self.assertIn(8, head[:first_slice], f"no PPS before the first IDR: {nals}")
        self.assertNotIn("Could not find codec parameters", trace)


    def test_audio_pause_does_not_stall_the_mux(self):
        """BACKLOG B3: the camera pausing its audio (DTX, Wi-Fi) must not stop
        the video. The real audio goes through a valve that is closed for 3 s;
        the mux must keep emitting (the silence source fills the gap) and the
        bus must stay clean."""
        fd = os.open(os.devnull, os.O_WRONLY)
        pipe = Gst.parse_launch(
            "videotestsrc is-live=true ! video/x-raw,width=160,height=120,framerate=20/1 ! "
            "x264enc tune=zerolatency key-int-max=20 speed-preset=ultrafast ! rtph264pay pt=101 config-interval=1 ! "
            "application/x-rtp,media=video,encoding-name=H264,payload=101,clock-rate=90000 ! "
            + b.VIDEO_BRANCH + " "
            "audiotestsrc is-live=true samplesperbuffer=960 ! audio/x-raw,rate=48000,channels=1 ! opusenc ! "
            "rtpopuspay pt=111 ! application/x-rtp,media=audio,encoding-name=OPUS,payload=111,clock-rate=48000 ! "
            "valve name=v ! " + b.AUDIO_BRANCH + " " + b.AUDIO_MIX + " " + b.MUX_CHAIN % fd)
        n = {"before": 0, "during": 0, "after": 0}
        phase = {"p": "before"}

        def pr(pad, info):
            n[phase["p"]] += 1
            return Gst.PadProbeReturn.OK
        pipe.get_by_name("out").get_static_pad("sink").add_probe(b.OUT_PROBE_TYPE, pr)
        pipe.set_state(Gst.State.PLAYING)
        time.sleep(3)
        pipe.get_by_name("v").set_property("drop", True)
        phase["p"] = "during"
        time.sleep(3)
        pipe.get_by_name("v").set_property("drop", False)
        phase["p"] = "after"
        time.sleep(2)
        msg = pipe.get_bus().pop_filtered(Gst.MessageType.ERROR)
        pipe.set_state(Gst.State.NULL)
        os.close(fd)
        self.assertIsNone(msg, msg.parse_error() if msg else "")
        self.assertGreater(n["before"], 50, n)
        self.assertGreater(n["during"], 50, f"mux stalled while audio paused: {n}")
        self.assertGreater(n["after"], 20, n)

    def test_audio_gate_holds_audio_until_first_video_keyframe(self):
        """Mixed audio is dropped on its way into the muxer while the session
        has not yet passed a video keyframe, and flows once it has (so the TS
        never opens with a stretch of audio alone, which made ffmpeg's probe
        give up on video)."""
        def run(kf_seen):
            sess = b.GstSession("test", {}, "rtsp://x/pub", "rtsp://x/read", audio=True)
            sess._kf_seen = kf_seen
            pipe = Gst.parse_launch(
                "audiotestsrc num-buffers=25 samplesperbuffer=960 ! audio/x-raw,rate=48000,channels=1 ! "
                "opusenc name=aenc ! fakesink")
            passed = []
            src = pipe.get_by_name("aenc").get_static_pad("src")
            src.add_probe(Gst.PadProbeType.BUFFER, sess._on_mix_out, None)
            src.add_probe(Gst.PadProbeType.BUFFER,
                          lambda pad, info: passed.append(1) or Gst.PadProbeReturn.OK)
            pipe.set_state(Gst.State.PLAYING)
            msg = pipe.get_bus().timed_pop_filtered(
                10 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
            pipe.set_state(Gst.State.NULL)
            self.assertEqual(msg.type, Gst.MessageType.EOS)
            return len(passed), sess._pre_kf_audio_dropped
        passed, dropped = run(False)       # opusenc may add a lookahead packet
        self.assertEqual(passed, 0)
        self.assertGreaterEqual(dropped, 25)
        passed, dropped = run(True)
        self.assertEqual(dropped, 0)
        self.assertGreaterEqual(passed, 25)

    def test_build_chain_strings(self):
        self.assertIn("fd=7", b.build_chain(7, False))
        self.assertNotIn("rtpopusdepay", b.build_chain(7, False))
        self.assertIn("mux.sink_65", b.build_chain(7, True))
        self.assertIn("mpegtsmux", b.build_chain(7, False))
        # the audio track exists even without a camera audio pad (BACKLOG C4)
        self.assertIn("audiomixer", b.build_chain(7, False))
        self.assertIn("mux.sink_65", b.build_chain(7, False))
        self.assertIn("-map", b.GstSession("d", {}, "rtsp://x", "rtsp://x", audio=False)._ffmpeg_cmd(False))
        self.assertIn("0:a", b.GstSession("d", {}, "rtsp://x", "rtsp://x", audio=False)._ffmpeg_cmd(False))


class Pll(unittest.TestCase):
    """Bursty arrivals (as measured on the wire) in, smooth monotonic clock out."""

    def _run(self, fps, burst, pause_ms, seconds, jitter_ms=0):
        import random
        rnd = random.Random(1)
        pll = b._Pll(33_000_000)
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
        pll = b._Pll(33_000_000)
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
        # the backlog burst after the stall must not be squeezed into a few ms
        burst = d[300:360]
        self.assertTrue(all(x >= 11_000_000 for x in burst), min(burst))
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

    def test_endpoint_allow_list(self):
        ok = ["wss://v-abc123.kinesisvideo.ca-central-1.amazonaws.com",
              "https://r-abc123.kinesisvideo.us-west-2.amazonaws.com",
              "wss://m-d73cdb17.kinesisvideo.ca-central-1.amazonaws.com/"]
        bad = ["wss://evil.example.com", "wss://kinesisvideo.ca-central-1.amazonaws.com.evil.net",
               "http://v-abc.kinesisvideo.ca-central-1.amazonaws.com", "ws://10.13.37.3:1234",
               None, 42, "wss://v-abc.kinesisvideo.ca-central-1.amazonaws.com:8443"]
        for u in ok:
            self.assertTrue(b.endpoint_allowed(u), u)
        for u in bad:
            self.assertFalse(b.endpoint_allowed(u), u)
        cfg = {"wss_endpoint": ok[0], "https_endpoint": ok[1]}
        self.assertIsNone(b.check_endpoints(cfg))
        self.assertIn("https_endpoint", b.check_endpoints({**cfg, "https_endpoint": bad[0]}))
        self.assertIn("missing", b.check_endpoints({"https_endpoint": ok[1]}))

    def test_shared_secret(self):
        class Req:
            def __init__(self, **h):
                self.headers = h
        saved = b.BRIDGE_TOKEN
        try:
            b.BRIDGE_TOKEN = ""
            self.assertTrue(b.authorized(Req()))
            b.BRIDGE_TOKEN = "s3cret"
            self.assertFalse(b.authorized(Req()))
            self.assertFalse(b.authorized(Req(**{"X-Bridge-Token": "wrong"})))
            self.assertTrue(b.authorized(Req(**{"X-Bridge-Token": "s3cret"})))
        finally:
            b.BRIDGE_TOKEN = saved

    def test_device_id_regex(self):
        self.assertTrue(b.DEVICE_ID_RE.fullmatch("db-C0F5358935AC"))
        for bad in ("a/b", "a b", "x%0d%0a", "a?b", "", "x" * 65):
            self.assertIsNone(b.DEVICE_ID_RE.fullmatch(bad), bad)


if __name__ == "__main__":
    unittest.main()

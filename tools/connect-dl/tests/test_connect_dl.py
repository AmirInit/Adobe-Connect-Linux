"""Tests for connect-dl.  Run with:  python3 -m unittest discover tools/connect-dl/tests"""

import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from connect_dl.archive import Role, Stream, unpack
from connect_dl.flv import scan_flv
from connect_dl.index import parse_events
from connect_dl.media import atempo_chain, build_audio_filter, build_video_filter
from connect_dl.player import PlayerSources, write_player
from connect_dl.urls import InvalidLink, parse_link


# --------------------------------------------------------------- FLV builder

def _amf_str(s):
    b = s.encode()
    return struct.pack(">H", len(b)) + b


def _tag(kind, ts, payload):
    return (
        bytes([kind])
        + len(payload).to_bytes(3, "big")
        + (ts & 0xFFFFFF).to_bytes(3, "big")
        + bytes([(ts >> 24) & 0xFF])
        + b"\x00\x00\x00"
        + payload
        + struct.pack(">I", 11 + len(payload))
    )


def make_flv(path, *, start_ms, dur_ms, audio=None, video=None, w=0, h=0):
    """A structurally valid FLV with the given codecs and timestamp range."""
    props = {"duration": dur_ms / 1000.0}
    if w:
        props.update(width=float(w), height=float(h))
    meta = b"\x02" + _amf_str("onMetaData") + b"\x08" + struct.pack(">I", len(props))
    for key, value in props.items():
        meta += _amf_str(key) + b"\x00" + struct.pack(">d", value)
    meta += struct.pack(">H", 0) + b"\x09"

    flags = (4 if audio else 0) | (1 if video else 0)
    buf = b"FLV\x01" + bytes([flags]) + struct.pack(">I", 9) + struct.pack(">I", 0)
    buf += _tag(18, 0, meta)
    for i in range(5):
        ts = start_ms + i * (dur_ms // 5)
        if video:
            buf += _tag(9, ts, bytes([0x10 | video]))
        if audio:
            buf += _tag(8, ts, bytes([(audio << 4) | 0x02]))
    Path(path).write_bytes(buf)


class TempRecording:
    """A throwaway directory that looks like an unpacked Connect recording."""

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name)
        return self

    def __exit__(self, *exc):
        self._tmp.cleanup()


# ------------------------------------------------------------------- tests

class TestUrls(unittest.TestCase):
    def test_recording_link(self):
        link = parse_link("https://acc.example.edu/p8fj3k2la9x/?pbMode=normal")
        self.assertEqual(link.origin, "https://acc.example.edu")
        self.assertEqual(link.url_path, "p8fj3k2la9x")
        self.assertEqual(link.base, "https://acc.example.edu/p8fj3k2la9x/")

    def test_connectpro_scheme_becomes_https(self):
        link = parse_link("connectpro://vadavc41.ec.iau.ir/l993retztu2a/?session=abc")
        self.assertEqual(link.origin, "https://vadavc41.ec.iau.ir")
        self.assertEqual(link.url_path, "l993retztu2a")
        self.assertEqual(link.session, "abc")

    def test_bare_host_gets_a_scheme(self):
        self.assertEqual(parse_link("acc.example.edu/p123abc").url_path, "p123abc")

    def test_api_path_is_not_mistaken_for_a_sco(self):
        link = parse_link("https://acc.example.edu/api/xml?action=sco-info&sco-id=12345")
        self.assertIsNone(link.url_path)
        self.assertEqual(link.sco_id, "12345")

    def test_asset_url(self):
        link = parse_link("https://acc.example.edu/p123abc/")
        self.assertEqual(
            link.asset("output/f.zip?download=zip"),
            "https://acc.example.edu/p123abc/output/f.zip?download=zip",
        )

    def test_empty_is_rejected(self):
        with self.assertRaises(InvalidLink):
            parse_link("   ")


class TestFlv(unittest.TestCase):
    def test_reads_codecs_and_metadata(self):
        with TempRecording() as rec:
            f = rec.path / "cameraVoip_1_1.flv"
            make_flv(f, start_ms=0, dur_ms=25000, audio=6, video=4, w=320, h=240)
            info = scan_flv(f)
            self.assertTrue(info.ok)
            self.assertEqual(info.audio_codec, "nellymoser")
            self.assertEqual(info.video_codec, "vp6")
            self.assertEqual((info.width, info.height), (320, 240))
            self.assertEqual(info.duration_ms, 25000)
            self.assertTrue(info.needs_transcode)

    def test_non_flv_is_reported_not_raised(self):
        with TempRecording() as rec:
            f = rec.path / "notes.txt"
            f.write_text("hello")
            self.assertEqual(scan_flv(f).error, "not an FLV file")


class TestOffsets(unittest.TestCase):
    """The alignment rules.  Getting these wrong reorders the lecture."""

    def test_meeting_relative_timestamps_are_trusted_including_zero(self):
        # Regression: a segment starting at t=0 is *resolved*, not unknown.
        # Treating 0 as "no information" pushed the first half of the lecture
        # to the end, behind the second half.
        with TempRecording() as rec:
            make_flv(rec.path / "cameraVoip_1_1.flv", start_ms=0, dur_ms=25000, audio=6)
            make_flv(rec.path / "cameraVoip_1_2.flv", start_ms=35000, dur_ms=25000, audio=6)
            archive = unpack(rec.path, rec.path)
            offsets = {s.name: s.offset_ms for s in archive.streams}
            self.assertEqual(offsets["cameraVoip_1_1.flv"], 0)
            self.assertEqual(offsets["cameraVoip_1_2.flv"], 35000)
            # the gap survives - it is a real silence in the lecture
            self.assertEqual(archive.duration_ms, 60000)

    def test_per_segment_timestamps_are_laid_end_to_end(self):
        # When every segment restarts at zero the timestamps say nothing, so
        # consecutive microphone segments must not stack on top of each other.
        with TempRecording() as rec:
            make_flv(rec.path / "cameraVoip_1_1.flv", start_ms=0, dur_ms=25000, audio=6)
            make_flv(rec.path / "cameraVoip_1_2.flv", start_ms=0, dur_ms=25000, audio=6)
            archive = unpack(rec.path, rec.path)
            offsets = sorted(s.offset_ms for s in archive.streams)
            self.assertEqual(offsets, [0, 25000])

    def test_timeline_is_shifted_back_to_zero(self):
        with TempRecording() as rec:
            make_flv(rec.path / "screenshare_1_1.flv", start_ms=30000, dur_ms=60000, video=3, w=1024, h=768)
            make_flv(rec.path / "cameraVoip_1_1.flv", start_ms=30000, dur_ms=60000, audio=6)
            archive = unpack(rec.path, rec.path)
            self.assertEqual(min(s.offset_ms for s in archive.streams), 0)

    def test_roles_and_audio_source_preference(self):
        with TempRecording() as rec:
            make_flv(rec.path / "screenshare_1_1.flv", start_ms=0, dur_ms=60000, video=3, w=1024, h=768)
            make_flv(rec.path / "cameraVoip_1_1.flv", start_ms=0, dur_ms=60000, audio=6, video=4, w=320, h=240)
            make_flv(rec.path / "mainstream.flv", start_ms=0, dur_ms=60000, audio=6, video=3, w=1024, h=768)
            archive = unpack(rec.path, rec.path)
            # the camera is preferred over the server's re-encoded composite
            self.assertEqual([s.name for s in archive.audio_streams], ["cameraVoip_1_1.flv"])
            self.assertEqual([s.name for s in archive.video_streams], ["screenshare_1_1.flv"])
            roles = {s.name: s.role for s in archive.streams}
            self.assertEqual(roles["screenshare_1_1.flv"], Role.SCREENSHARE)
            self.assertEqual(roles["mainstream.flv"], Role.MAIN)

    def test_mainstream_is_the_fallback_when_nothing_else_has_audio(self):
        with TempRecording() as rec:
            make_flv(rec.path / "mainstream.flv", start_ms=0, dur_ms=60000, audio=6, video=3, w=800, h=600)
            archive = unpack(rec.path, rec.path)
            self.assertEqual([s.name for s in archive.audio_streams], ["mainstream.flv"])


class TestMedia(unittest.TestCase):
    def test_atempo_chain_multiplies_out_to_the_request(self):
        for speed in (0.25, 0.5, 0.75, 1.5, 2, 2.5, 3, 4, 8):
            chain = atempo_chain(speed)
            product = 1.0
            for part in chain.split(","):
                product *= float(part.split("=")[1])
            self.assertAlmostEqual(product, speed, places=9, msg=f"speed={speed}")
            for part in chain.split(","):
                factor = float(part.split("=")[1])
                self.assertTrue(0.5 <= factor <= 2.0, f"{factor} out of atempo range")

    def test_atempo_identity(self):
        self.assertEqual(atempo_chain(1.0), "anull")

    def test_audio_filter_delays_later_segments(self):
        streams = [
            Stream(path=Path("a.flv"), role=Role.CAMERA, offset_ms=0),
            Stream(path=Path("b.flv"), role=Role.CAMERA, offset_ms=2670000),
        ]
        graph, label = build_audio_filter(streams)
        self.assertEqual(label, "[aout]")
        self.assertIn("adelay=2670000:all=1", graph)
        self.assertIn("amix=inputs=2", graph)
        # the first stream must not be delayed
        self.assertNotIn("[0:a]aresample=async=1:first_pts=0,aformat=sample_fmts=fltp:"
                         "sample_rates=48000:channel_layouts=mono,adelay", graph)

    def test_video_filter_opens_each_file_once(self):
        cam = Stream(path=Path("cameraVoip_1_1.flv"), role=Role.CAMERA, offset_ms=0)
        screen = Stream(path=Path("screenshare_1_1.flv"), role=Role.SCREENSHARE, offset_ms=0)
        graph, inputs, vlabel, alabel = build_video_filter(
            [screen], [cam], [cam], width=1280, height=720, fps=5, duration_s=60.0
        )
        # the camera supplies both picture and sound but is a single input
        self.assertEqual(len(inputs), 2)
        self.assertEqual(vlabel, "[vout]")
        self.assertEqual(alabel, "[aout]")
        self.assertNotIn("[base]", graph)

    def test_video_filter_offsets_use_tpad(self):
        late = Stream(path=Path("s2.flv"), role=Role.SCREENSHARE, offset_ms=35000)
        graph, _, _, _ = build_video_filter(
            [late], [], [], width=1280, height=720, fps=5, duration_s=60.0
        )
        self.assertIn("tpad=start_duration=35.000", graph)


class TestEvents(unittest.TestCase):
    def test_shift_dedupe_and_range_filtering(self):
        with TempRecording() as rec:
            (rec.path / "ftchat.xml").write_text(
                '<chat><message time="65000" from="Prof">&lt;b&gt;Hello&lt;/b&gt; all</message>'
                '<message time="65000" from="Prof">&lt;b&gt;Hello&lt;/b&gt; all</message></chat>'
            )
            (rec.path / "indexstream.xml").write_text(
                '<index><event time="30000" name="Start"/>'
                '<event time="99999999" name="past the end"/></index>'
            )
            events = parse_events(
                [rec.path / "ftchat.xml", rec.path / "indexstream.xml"],
                shift_ms=30000, duration_ms=600000,
            )
            self.assertEqual([m.label for m in events.markers], ["Start"])
            self.assertEqual(len(events.chat), 1)                 # deduped
            self.assertEqual(events.chat[0].message, "Hello all")  # markup stripped
            self.assertEqual(events.chat[0].time_ms, 35000)        # shifted


class TestPlayer(unittest.TestCase):
    def test_writes_a_self_contained_page(self):
        with TempRecording() as rec:
            out = write_player(
                rec.path / "play.html",
                title='Lecture <7> & "friends"',
                sources=PlayerSources(video="v.mp4", audio="a.mp3"),
                events=parse_events([]),
                duration_s=60.0,
            )
            html = out.read_text()
            self.assertNotIn("__TITLE__", html)
            self.assertNotIn("/*__DATA__*/null", html)
            self.assertIn("&lt;7&gt;", html)          # title escaped
            self.assertIn("playbackRate", html)       # the whole point
            self.assertIn("preservesPitch", html)


class TestArchiveSafety(unittest.TestCase):
    def test_zip_slip_is_refused(self):
        import zipfile
        with TempRecording() as rec:
            evil = rec.path / "evil.zip"
            with zipfile.ZipFile(evil, "w") as zf:
                zf.writestr("../escaped.txt", "nope")
            with self.assertRaises(ValueError):
                unpack(evil, rec.path / "out")


if __name__ == "__main__":
    unittest.main(verbosity=2)

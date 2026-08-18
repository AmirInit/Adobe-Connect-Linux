"""Tests for connect-dl.  Run with:  python3 -m unittest discover tools/connect-dl/tests"""

import contextlib
import io
import os
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from connect_dl.api import (
    AuthError, ConnectClient, ConnectError, _permanent_reason, looks_like_login_page,
)
from connect_dl.cli import _client
from connect_dl.archive import Role, Stream, unpack
from connect_dl.fetch import _MP4_CANDIDATES, _ZIP_CANDIDATES, download_recording
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


# ------------------------------------------------- regressions found against
# a real server (Adobe Connect 10.8.0, vadavc41.ec.iau.ir)

# Trimmed from what that server actually returns for an unauthenticated
# request to <room>/output/recording.zip?download=zip - note the HTTP status is
# 200 and the content-type is text/html, so nothing but the body reveals it.
LOGIN_PAGE = (
    b'<html lang="en">\r\n<head>\r\n<title>Adobe Connect Central Login</title>\r\n'
    b'<meta http-equiv="X-UA-Compatible" content="IE=edge">\r\n'
    b'<script src="/common/scripts/showContent.js?ver=10.8.0"></script>'
    b'<script type="text/javascript" src="/common/scripts/breezeUI.js?ver=10.8.0">'
    b'</script>\r\n</head><body><form name="login"></form></body></html>'
)

HOLDING_PAGE = (
    b'<html><head><title>Please wait</title></head>'
    b'<body>Your recording is being prepared.</body></html>'
)


class _FakeResponse:
    def __init__(self, body, content_type):
        self._buf = io.BytesIO(body)
        self.headers = {"Content-Type": content_type, "Content-Length": str(len(body))}

    def read(self, n=-1):
        return self._buf.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeClient:
    """Just enough ConnectClient for fetch._try_download."""

    def __init__(self, body, content_type="text/html;charset=UTF-8"):
        self.body, self.content_type = body, content_type
        self.calls = 0

    def _open(self, url, **kw):
        self.calls += 1
        return _FakeResponse(self.body, self.content_type)


class TestLoginPageDetection(unittest.TestCase):
    def test_recognises_the_real_login_page(self):
        self.assertTrue(looks_like_login_page(LOGIN_PAGE))

    def test_holding_page_is_not_a_login_page(self):
        # Must stay False, or a genuinely-still-building zip would abort early.
        self.assertFalse(looks_like_login_page(HOLDING_PAGE))

    def test_zip_bytes_are_not_a_login_page(self):
        self.assertFalse(looks_like_login_page(b"PK\x03\x04" + os.urandom(64)))


class TestDownloadFailsFastOnLogin(unittest.TestCase):
    """The bug: a login page was read as 'still preparing' and polled for 30 min."""

    def test_login_page_aborts_immediately(self):
        client = _FakeClient(LOGIN_PAGE)
        link = parse_link("https://acc.example.edu/l3fw0y0rs38h/")
        with TempRecording() as rec:
            with self.assertRaises(AuthError) as caught:
                download_recording(
                    client, link, rec.path, poll_timeout=300, poll_interval=15,
                )
        message = str(caught.exception)
        self.assertIn("login page", message)
        self.assertIn("--session", message)
        # One candidate URL, one request - not a poll loop.
        self.assertEqual(client.calls, 1)

    def test_holding_page_still_polls_then_gives_up(self):
        client = _FakeClient(HOLDING_PAGE)
        link = parse_link("https://acc.example.edu/p8fj3k2la9x/")
        with TempRecording() as rec:
            with self.assertRaises(ConnectError) as caught:
                download_recording(
                    client, link, rec.path, poll_timeout=1, poll_interval=0,
                )
        # A real holding page must NOT be treated as an auth failure - it is
        # retried, and only the timeout ends the run.
        self.assertNotIsInstance(caught.exception, AuthError)
        self.assertGreaterEqual(client.calls, len(_MP4_CANDIDATES) + len(_ZIP_CANDIDATES))
        self.assertIn("could not obtain a downloadable recording", str(caught.exception))


class TestPermanentTransportErrors(unittest.TestCase):
    """The bug: unresolvable hosts and bad certs were retried 4x over 15s."""

    def test_dns_failure_is_permanent(self):
        wrapped = urllib.error.URLError(socket.gaierror(-2, "Name or service not known"))
        self.assertIn("could not be resolved", _permanent_reason(wrapped))

    def test_cert_failure_is_permanent_when_wrapped(self):
        err = ssl.SSLCertVerificationError("self signed certificate")
        self.assertIn("--insecure", _permanent_reason(urllib.error.URLError(err)))

    def test_cert_failure_is_permanent_when_raised_directly(self):
        # ssl.SSLError.reason is a str, so this only works if the exception
        # itself is inspected and not just its .reason.
        err = ssl.SSLCertVerificationError("self signed certificate")
        err.reason = "CERTIFICATE_VERIFY_FAILED"
        self.assertIn("--insecure", _permanent_reason(err))

    def test_transient_errors_are_still_retried(self):
        self.assertIsNone(_permanent_reason(TimeoutError("timed out")))
        self.assertIsNone(_permanent_reason(urllib.error.URLError(ConnectionResetError())))

    def test_dns_failure_does_not_retry(self):
        client = ConnectClient(origin="https://nope.invalid")
        with mock.patch.object(
            client._opener, "open",
            side_effect=urllib.error.URLError(socket.gaierror(-2, "Name or service not known")),
        ) as opened:
            with self.assertRaises(ConnectError):
                client._open("https://nope.invalid/api/xml")
        self.assertEqual(opened.call_count, 1)


class _Args:
    """Stand-in for the argparse namespace _client() reads."""

    def __init__(self, **kw):
        self.url = kw.pop("url", "https://acc.example.edu/l3fw0y0rs38h/")
        self.session = kw.pop("session", None)
        self.user = kw.pop("user", None)
        self.password = kw.pop("password", None)
        self.insecure = kw.pop("insecure", True)
        self.__dict__.update(kw)


class TestInvalidSessionIsReported(unittest.TestCase):
    """The bug: an expired BREEZESESSION behaved exactly like passing nothing."""

    def test_session_that_is_not_logged_in_raises(self):
        # check_session() returns None for a cookie the server treats as
        # anonymous - which is what an expired cookie looks like.
        with mock.patch.object(ConnectClient, "check_session", return_value=None):
            with self.assertRaises(AuthError) as caught:
                _client(_Args(session="BOGUS123"))
        self.assertIn("expired", str(caught.exception))

    def test_valid_session_is_accepted(self):
        with mock.patch.object(ConnectClient, "check_session", return_value="me@x.edu"):
            with contextlib.redirect_stdout(io.StringIO()):
                client, link = _client(_Args(session="GOOD"))
        self.assertEqual(link.url_path, "l3fw0y0rs38h")


class TestCliEntryPoints(unittest.TestCase):
    """The bug: cli.py had no __main__ guard, so this printed nothing, exit 0."""

    def _run(self, *args):
        return subprocess.run(
            [sys.executable, "-m", *args, "--help"],
            cwd=str(Path(__file__).resolve().parents[1]),
            capture_output=True, text=True, timeout=60,
        )

    def test_module_and_package_entry_points_both_print_usage(self):
        for target in ("connect_dl", "connect_dl.cli"):
            with self.subTest(target=target):
                result = self._run(target)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("usage: connect-dl", result.stdout)
                self.assertIn("get", result.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)

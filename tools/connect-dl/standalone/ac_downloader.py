#!/usr/bin/env python3
"""Adobe Connect recording downloader - the whole thing in one file.

This is the zero-setup copy: no package, no install, nothing to clone.  Drop it
on any machine with Python 3.9+ and ffmpeg and run it.

    python3 ac_downloader.py
    python3 ac_downloader.py "https://host/abc123/?session=TOKEN&proto=true"

It downloads the recording archive, works out where each stream belongs on the
lecture timeline, levels the microphones against each other, and produces:

    lecture.mp3   the voices as one continuous track
    lecture.mp4   screen share + webcam picture-in-picture + audio, when the
                  recording actually contains a picture
    play.html     an offline player with a 0.5x-16x speed control
    chat.txt      the chat transcript, when the archive carries one

The maintained version of all this is the connect_dl package next to this file,
which has more of everything - a from-scratch FLV parser that does not need
ffprobe, a fuller player, a test suite.  This file exists for the case where
copying one file is easier than copying a directory.  The two agree on the
decisions that matter (link parsing, which files are metadata, when timestamps
can be trusted); tests/test_connect_dl.py checks that they still do.

Standard library only.  ffmpeg and ffprobe on PATH are the one requirement.
"""

from __future__ import annotations

import argparse
import getpass
import html as html_mod
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import webbrowser
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from urllib.parse import parse_qs, urlparse

VERSION = "2.0"

# FLV by extension, but they carry chat, the whiteboard draw-commands, the seek
# index and the transcript - not media.  Handing them to ffmpeg is what makes a
# render "succeed" while producing the wrong thing.
METADATA_PREFIXES = ("ftchat", "ftcontent", "indexstream", "transcriptstream",
                     "ftnote", "ftpoll", "ftquestion", "ftshare")

# The asset name a real Connect 10.8 server serves.  The others are kept
# because other versions have used them; note that output/recording.mp4 answers
# with the *login page* on servers that do not have it, which is why the zips
# are tried first - see fetch_archive().
ASSET_CANDIDATES = ("output/class.zip?download=zip",
                    "output/recording.zip?download=zip",
                    "output/filename.zip?download=zip")

# Microphone levelling.  A lecturer's headset and a student on a laptop can
# differ by 30 dB, which is the difference between clear and inaudible.
TARGET_MEAN_DB = -20.0    # where a well-levelled voice sits
PEAK_CEILING_DB = -1.5    # never boost past this, to avoid clipping
MAX_BOOST_DB = 30.0       # a limit, so near-silence is not amplified into hiss
QUIET_THRESHOLD_DB = 8.0  # a segment raised more than this is worth reporting
SILENCE_FLOOR_DB = -70.0

# Timeline alignment.  See resolve_offsets().
MIN_SPREAD_MS = 30_000
SPREAD_FRACTION = 4

# Connect names microphone segments cameraVoip_<speaker>_<sequence>.flv.
SEQ_RE = re.compile(r"^([A-Za-z]+)_(\d+)_(\d+)", re.A)

USER_AGENT = f"ac_downloader/{VERSION}"


def die(message: str) -> "NoReturn":  # noqa: F821
    print(f"\n[!] {message}", file=sys.stderr)
    sys.exit(1)


# --------------------------------------------------------------------- link

class Link:
    """A Connect link split into the parts that matter.

    Accepts anything a person might paste: a recording link, a meeting-room
    link, a connectpro:// URL, with or without a ?session= token and with any
    amount of other query junk.
    """

    RESERVED = {"api", "admin", "system", "common", "content", "flash",
                "output", "swf", "app", "connect"}

    def __init__(self, raw: str):
        raw = raw.strip().strip('"').strip("'")
        for scheme in ("connectpro://", "connect://", "meeting://"):
            if raw.lower().startswith(scheme):
                rest = raw[len(scheme):]
                raw = rest if rest.lower().startswith(("http://", "https://")) else "https://" + rest
                break
        if not re.match(r"^https?://", raw, re.I):
            raw = "https://" + raw

        parsed = urlparse(raw)
        if not parsed.netloc:
            raise ValueError(f"no host found in {raw!r}")
        self.origin = f"{parsed.scheme}://{parsed.netloc}"

        self.handle = None
        for segment in parsed.path.split("/"):
            segment = segment.strip()
            if not segment or "." in segment:
                continue
            if segment.lower() in self.RESERVED:
                break
            self.handle = segment
            break

        # People paste the whole launcher URL, token and all.  A ?session= in
        # the query does not authenticate anything by itself - but it is the
        # right value to send as the Cookie header, so take it from there.
        query = parse_qs(parsed.query)
        self.session = None
        for key in ("session", "BREEZESESSION", "breezesession"):
            if query.get(key) and query[key][0].strip():
                self.session = query[key][0].strip()
                break

    @property
    def base(self) -> str:
        return f"{self.origin}/{self.handle}/" if self.handle else self.origin + "/"

    def asset(self, path: str) -> str:
        return self.base + path.lstrip("/")


# ----------------------------------------------------------------- download

LOGIN_MARKERS = (b"adobe connect central login", b"/common/scripts/breezeui.js",
                 b'name="login"', b"session-timeout")


def looks_like_login_page(raw: bytes) -> bool:
    """True if this is Connect's login page rather than a real answer.

    The server returns HTTP 200 with this page for an unauthenticated request,
    so the status code cannot be trusted and only the body tells the truth.  It
    must also stay distinguishable from the genuine "still building your zip"
    holding page, which *is* worth retrying.
    """
    head = raw[:4096].lower()
    if b"<html" not in head and not head.lstrip().startswith(b"<"):
        return False
    return any(marker in head for marker in LOGIN_MARKERS)


def open_url(url: str, session: str | None, *, resume_from: int = 0):
    headers = {"User-Agent": USER_AGENT}
    if session:
        headers["Cookie"] = f"BREEZESESSION={session}"
    if resume_from:
        headers["Range"] = f"bytes={resume_from}-"
    return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60)


def fetch_archive(link: Link, session: str | None, dest: Path) -> Path:
    """Download the recording archive, or explain precisely why we cannot."""
    saw_login = False
    for candidate in ASSET_CANDIDATES:
        url = link.asset(candidate)
        print(f"[*] trying {url}")
        try:
            response = open_url(url, session)
        except urllib.error.HTTPError as exc:
            print(f"    HTTP {exc.code}")
            continue
        except urllib.error.URLError as exc:
            print(f"    {exc.reason}")
            continue

        with response:
            ctype = (response.headers.get("Content-Type") or "").lower()
            # Read four bytes, never the body: probing an 80 MB archive must
            # not cost 80 MB.
            head = response.read(4)
            if "html" in ctype or head.startswith(b"<"):
                body = head + response.read(4096)
                if looks_like_login_page(body):
                    saw_login = True
                    print("    got the login page, not an archive")
                else:
                    print("    the server is still preparing the archive")
                continue
            if head[:2] != b"PK" and "zip" not in ctype:
                print(f"    unexpected content type: {ctype or 'unknown'}")
                continue

        zip_path = dest / "class_archive.zip"
        if download_with_resume(url, session, zip_path):
            return zip_path
        die(f"the download kept breaking before it finished.\n"
            f"  The partial file is kept at {zip_path} - rerun to resume it.")

    if saw_login:
        die("the server returned its login page instead of the archive.\n"
            "  The recording is not public and the session token was missing,\n"
            "  wrong, or expired.  Log into Connect in a browser, copy the\n"
            "  BREEZESESSION cookie (F12 -> Application -> Cookies) and pass it\n"
            "  with --session, or paste a link that still carries ?session=...")
    die("no downloadable archive was found at this address.\n"
        "  Check that the meeting was actually recorded, and that the link is\n"
        "  the one Connect gave you for it.")


def download_with_resume(url: str, session: str | None, zip_path: Path,
                         attempts: int = 5) -> bool:
    """Download to ``zip_path``, resuming after a dropped connection.

    Archives of tens of megabytes routinely die mid-transfer.  Each attempt
    continues from the bytes already on disk with a Range request rather than
    starting over, and the finished file is verified as a real zip.
    """
    print("[*] downloading archive")
    for attempt in range(1, attempts + 1):
        have = zip_path.stat().st_size if zip_path.exists() else 0
        try:
            with open_url(url, session, resume_from=have) as response:
                status = getattr(response, "status", 200)
                if have and status != 206:
                    # The server ignored the Range and is resending everything;
                    # appending would corrupt the file.
                    print("    server will not resume; starting again")
                    zip_path.unlink(missing_ok=True)
                    have = 0
                total = int(response.headers.get("Content-Length") or 0) + have
                if have:
                    print(f"    resuming from {have >> 20} MiB")
                complete = stream_to_file(response, zip_path, already=have, total=total)
        except urllib.error.HTTPError as exc:
            if exc.code == 416 and have:
                # We hold at least as much as the server has, yet it did not
                # verify: what is on disk is junk, not a resumable prefix.
                zip_path.unlink(missing_ok=True)
                continue
            print(f"    HTTP {exc.code}")
            return False
        except (urllib.error.URLError, OSError) as exc:
            complete = False
            print(f"\n[!] connection dropped ({exc})")

        if complete and zip_is_complete(zip_path):
            return True
        got = zip_path.stat().st_size if zip_path.exists() else 0
        if have and got <= have:
            zip_path.unlink(missing_ok=True)   # no progress: the partial is junk
        if attempt < attempts:
            print(f"[!] incomplete at {got >> 20} MiB "
                  f"(attempt {attempt}/{attempts}); resuming")
    return zip_is_complete(zip_path)


def stream_to_file(response, dest: Path, *, already: int, total: int) -> bool:
    """Append the response body to ``dest``.  False if the transfer broke."""
    written = already
    try:
        with open(dest, "ab" if already else "wb") as handle:
            while True:
                chunk = response.read(1 << 16)
                if not chunk:
                    break
                handle.write(chunk)
                written += len(chunk)
                if total:
                    print(f"\r    {written * 100 // max(total, 1):3d}%  "
                          f"{written >> 20} MiB", end="", flush=True)
    except (OSError, urllib.error.URLError) as exc:
        print(f"\n[!] transfer interrupted after {written >> 20} MiB: {exc}")
        return False
    finally:
        if total:
            print()
    return not total or written >= total


def zip_is_complete(zip_path: Path) -> bool:
    """True if the file is a structurally valid zip.

    A zip's central directory sits at the end of the file, so reading it is
    both cheap and exactly the check that catches a truncated download.
    """
    try:
        if not zip_path.exists() or zip_path.stat().st_size < 100_000:
            return False
        with zipfile.ZipFile(zip_path) as zf:
            return bool(zf.namelist())
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile):
        return False


# ------------------------------------------------------------------ archive

class Stream:
    def __init__(self, path: Path, info: dict):
        self.path = path
        self.name = path.name
        self.has_audio = info["has_audio"]
        self.has_video = info["has_video"]
        self.width = info["width"]
        self.height = info["height"]
        self.start_ms = info["start_ms"]
        self.duration_ms = info["duration_ms"]
        self.offset_ms = 0
        self.slot = 0
        self.mean_db: float | None = None
        self.max_db: float | None = None
        self.gain_db = 0.0

        low = self.name.lower()
        if low.startswith("cameravoip"):
            self.role = "camera"
        elif low.startswith("screenshare"):
            self.role = "screen"
        elif low.startswith("mainstream"):
            self.role = "main"
        else:
            self.role = "other"

        match = SEQ_RE.match(self.name)
        self.speaker = int(match.group(2)) if match else 0
        self.seq = int(match.group(3)) if match else 0

    @property
    def end_ms(self) -> int:
        return self.offset_ms + self.duration_ms

    def __repr__(self) -> str:
        kind = ("A" if self.has_audio else "-") + ("V" if self.has_video else "-")
        line = (f"{self.name:<28} spk{self.speaker} {kind}  at {ts(self.offset_ms)}"
                f"  for {self.duration_ms / 1000:7.1f}s")
        if self.mean_db is not None:
            line += f"  {self.mean_db:6.1f}dB"
            if self.gain_db >= 0.5:
                line += f" +{self.gain_db:.0f}"
        return line


def ts(ms: int) -> str:
    total = max(0, ms) // 1000
    return f"{total // 60:3d}:{total % 60:02d}"


def probe(path: Path) -> dict | None:
    """Read stream layout and timing with ffprobe."""
    cmd = ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    try:
        data = json.loads(out.stdout)
    except json.JSONDecodeError:
        return None

    streams = data.get("streams", [])
    fmt = data.get("format", {})
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    video = [s for s in streams if s.get("codec_type") == "video"]
    if not audio and not video:
        return None

    def as_ms(value, default=0):
        try:
            return int(round(float(value) * 1000))
        except (TypeError, ValueError):
            return default

    start_ms = as_ms(fmt.get("start_time"), 0)
    if start_ms <= 0:
        for s in streams:
            candidate = as_ms(s.get("start_time"), 0)
            if candidate > 0:
                start_ms = candidate
                break

    duration_ms = as_ms(fmt.get("duration"), 0)
    if duration_ms <= 0:
        duration_ms = max((as_ms(s.get("duration"), 0) for s in streams), default=0)

    return {
        "has_audio": bool(audio), "has_video": bool(video),
        "width": int(video[0].get("width") or 0) if video else 0,
        "height": int(video[0].get("height") or 0) if video else 0,
        "start_ms": max(0, start_ms), "duration_ms": max(0, duration_ms),
    }


def unpack(zip_path: Path, dest: Path) -> tuple[list[Stream], list[Path]]:
    """Extract the archive; return its media streams and its XML sidecars."""
    dest.mkdir(parents=True, exist_ok=True)
    print("[*] extracting archive")
    try:
        with zipfile.ZipFile(zip_path) as zf:
            root = str(dest.resolve())
            for member in zf.infolist():
                if not str((dest / member.filename).resolve()).startswith(root):
                    die(f"archive tried to write outside the output directory: {member.filename}")
            zf.extractall(dest)
    except zipfile.BadZipFile:
        die("the downloaded archive is corrupt (BadZipFile).")

    media: list[Stream] = []
    metadata: list[str] = []
    unreadable: list[str] = []

    for path in sorted(dest.rglob("*.flv")):
        if path.name.lower().startswith(METADATA_PREFIXES):
            metadata.append(path.name)
            continue
        info = probe(path)
        if info is None:
            unreadable.append(f"{path.name} ({path.stat().st_size // 1024} KiB)")
            continue
        media.append(Stream(path, info))

    if metadata:
        print(f"[*] metadata, not media: {', '.join(metadata)}")
    if unreadable:
        print(f"[!] could not decode: {', '.join(unreadable)}")
        print("    a few KiB means an empty segment and is harmless;")
        print("    several MiB means a real stream was lost.")
    if not media:
        die("the archive contains no playable audio or video streams.")
    return media, sorted(dest.rglob("*.xml"))


# ----------------------------------------------------------------- timeline

def resolve_offsets(streams: list[Stream], layout: str = "auto") -> str:
    """Place each stream on the lecture timeline; returns the layout used.

    Connect archives come in two shapes.  In one, every FLV carries its real
    position within the meeting.  In the other, each segment restarts its clock
    near zero and the timestamps say nothing about ordering.

    Telling them apart by "is any start time non-zero?" is wrong, and wrong in
    a way that ruins the output: on a real 38-minute recording six microphone
    segments all began within 4.4 seconds of each other - start-up jitter - yet
    every one of those starts was non-zero, so all six landed on top of each
    other and everybody talked at once.

    The question is one of magnitude.  Real meeting positions spread across the
    whole recording, so the gap between the first and last start is comparable
    to the material; jitter is a rounding error next to it.  The stamps are only
    trusted when their spread reaches a quarter of the longest single track,
    and at least 30 seconds.
    """
    if layout not in ("auto", "timeline", "sequential"):
        raise ValueError(f"unknown layout {layout!r}")

    per_role: dict[str, int] = {}
    for s in streams:
        per_role[s.role] = per_role.get(s.role, 0) + s.duration_ms
    content_ms = max(per_role.values(), default=0)
    starts = [s.start_ms for s in streams]
    spread = max(starts, default=0) - min(starts, default=0)
    threshold = max(MIN_SPREAD_MS, content_ms // SPREAD_FRACTION)

    if layout == "timeline":
        stamped = True
    elif layout == "sequential":
        stamped = False
    else:
        stamped = spread >= threshold
        print(f"[*] segment starts span {spread / 1000:.1f}s against a "
              f"{threshold / 1000:.1f}s threshold")

    if stamped:
        for s in streams:
            s.offset_ms = s.start_ms
        mode = "timeline"
    else:
        # Lay segments end to end in the order Connect numbered them, so no two
        # speakers overlap.  Picture and sound get separate cursors: a screen
        # share runs underneath the audio rather than after it.
        cursor: dict[str, int] = {}
        for s in sorted(streams, key=lambda x: (x.seq, x.name)):
            track = "picture" if s.role in ("screen", "main") else "sound"
            s.offset_ms = cursor.get(track, 0)
            cursor[track] = s.offset_ms + s.duration_ms
        mode = "sequential"

    base = min((s.offset_ms for s in streams), default=0)
    for s in streams:
        s.offset_ms -= base
    return mode


def describe_layout(mode: str) -> None:
    if mode == "timeline":
        print("[*] layout: meeting timestamps - segments keep their real "
              "positions and gaps stay silent")
    else:
        print("[*] layout: sequential - the timestamps were jitter, not "
              "positions, so segments play back to back in recorded order")
        print("    (if that sounds out of order, try --layout timeline)")


# ------------------------------------------------------------------- levels

def measure_level(path: Path) -> tuple[float, float] | None:
    """Return (mean_dB, peak_dB) for a file's audio, via one decode pass."""
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-i", str(path),
           "-af", "volumedetect", "-f", "null", "-"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    except (OSError, subprocess.TimeoutExpired):
        return None
    mean = re.search(r"mean_volume:\s*(-?[\d.]+) dB", out.stderr)
    peak = re.search(r"max_volume:\s*(-?[\d.]+) dB", out.stderr)
    if not mean or not peak:
        return None
    return float(mean.group(1)), float(peak.group(1))


def plan_gain(mean_db: float, max_db: float) -> float:
    """How much to raise a segment measuring ``mean_db`` / ``max_db``.

    Chosen from the mean rather than the peak, so one stray click cannot hold a
    whole quiet segment down; capped by the peak headroom so boosting cannot
    clip, and by an absolute limit so room tone is not amplified into hiss.
    Never negative: this raises quiet mics, it does not duck good ones.
    """
    if mean_db <= SILENCE_FLOOR_DB:
        return 0.0
    return round(max(0.0, min(TARGET_MEAN_DB - mean_db,
                              PEAK_CEILING_DB - max_db, MAX_BOOST_DB)), 1)


def level_streams(streams: list[Stream]) -> list[Stream]:
    """Measure every microphone and give each its own gain."""
    audible = [s for s in streams if s.has_audio]
    for s in audible:
        measured = measure_level(s.path)
        if measured is None:
            continue
        s.mean_db, s.max_db = measured
        s.gain_db = plan_gain(s.mean_db, s.max_db)
    return [s for s in audible if s.gain_db >= QUIET_THRESHOLD_DB]


# ------------------------------------------------------------------- render

def run_ffmpeg(cmd: list[str], *, what: str) -> bool:
    print(f"[*] {what}")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-12:])
        print(f"[!] ffmpeg failed while {what}:\n{tail}")
        return False
    return True


def atempo_chain(speed: float) -> str:
    """Arbitrary speed as a chain of in-range atempo filters."""
    if speed <= 0:
        raise ValueError("speed must be positive")
    if abs(speed - 1.0) < 1e-6:
        return "anull"
    factors: list[float] = []
    rest = speed
    while rest > 2.0:
        factors.append(2.0)
        rest /= 2.0
    while rest < 0.5:
        factors.append(0.5)
        rest /= 0.5
    factors.append(rest)
    return ",".join(f"atempo={f:g}" for f in factors)


AFMT = "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=mono"


def audio_branches(streams: list[Stream], label: str) -> tuple[list[str], list[str]]:
    """Filter branches placing each segment at its own point on the timeline."""
    parts, labels = [], []
    for i, s in enumerate(streams):
        chain = ["aresample=async=1:first_pts=0", AFMT]
        if s.gain_db >= 0.1:
            # Levelling comes first, so the delay padding stays true silence.
            chain.append(f"volume={s.gain_db:.1f}dB")
        if s.offset_ms > 0:
            chain.append(f"adelay={s.offset_ms}:all=1")
        tag = f"{label}{i}"
        parts.append(f"[{s.slot}:a]{','.join(chain)}[{tag}]")
        labels.append(f"[{tag}]")
    return parts, labels


def render_audio(streams: list[Stream], out: Path, speed: float = 1.0) -> bool:
    """One continuous track, every voice where it belongs."""
    cmd = ["ffmpeg", "-y", "-nostdin"]
    for i, s in enumerate(streams):
        s.slot = i
        cmd += ["-i", str(s.path)]

    parts, labels = audio_branches(streams, "a")
    if len(labels) == 1:
        parts.append(f"{labels[0]}anull[mix]")
    else:
        parts.append(f"{''.join(labels)}amix=inputs={len(labels)}"
                     f":duration=longest:dropout_transition=0:normalize=0[mix]")
    parts.append(f"[mix]{atempo_chain(speed)}[aout]")

    cmd += ["-filter_complex", ";".join(parts), "-map", "[aout]",
            "-c:a", "libmp3lame", "-q:a", "4", str(out)]
    return run_ffmpeg(cmd, what=f"mixing audio -> {out.name}")


def render_video(screens: list[Stream], cams: list[Stream], audio: list[Stream],
                 out: Path, *, width=1280, height=720, fps=5, crf=28) -> bool:
    duration_s = max((s.end_ms for s in screens + cams + audio), default=1000) / 1000.0
    inputs: list[str] = []
    slots: dict[str, int] = {}

    def slot(s: Stream) -> int:
        """Input index, reusing a file already opened for another purpose."""
        key = str(s.path)
        if key not in slots:
            inputs.append(key)
            slots[key] = len(inputs)      # slot 0 is the black canvas
        return slots[key]

    parts: list[str] = []
    current = "[0:v]"

    def overlay(s: Stream, target_w: int, position: str) -> None:
        nonlocal current
        idx = slot(s)
        chain = [f"scale={target_w}:-2:force_original_aspect_ratio=decrease",
                 f"fps={fps}", "setpts=PTS-STARTPTS"]
        if s.offset_ms > 0:
            chain.append(f"tpad=start_duration={s.offset_ms / 1000:.3f}"
                         f":start_mode=add:color=black")
        parts.append(f"[{idx}:v]{','.join(chain)}[v{idx}]")
        parts.append(f"{current}[v{idx}]overlay={position}"
                     f":eof_action=pass:repeatlast=0[c{idx}]")
        current = f"[c{idx}]"

    for s in screens:
        overlay(s, width, "(W-w)/2:(H-h)/2")
    for s in cams:
        overlay(s, width // 4, "W-w-16:H-h-16")
    parts.append(f"{current}format=yuv420p[vout]")

    for s in audio:
        s.slot = slot(s)
    abranches, alabels = audio_branches(audio, "pa")
    parts += abranches
    if len(alabels) == 1:
        parts.append(f"{alabels[0]}anull[aout]")
    elif alabels:
        parts.append(f"{''.join(alabels)}amix=inputs={len(alabels)}"
                     f":duration=longest:dropout_transition=0:normalize=0[aout]")

    cmd = ["ffmpeg", "-y", "-nostdin", "-f", "lavfi", "-i",
           f"color=c=black:s={width}x{height}:r={fps}:d={duration_s:.3f}"]
    for path in inputs:
        cmd += ["-i", path]
    cmd += ["-filter_complex", ";".join(parts), "-map", "[vout]"]
    if alabels:
        cmd += ["-map", "[aout]", "-c:a", "aac", "-b:a", "96k"]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
            "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            "-t", f"{duration_s:.3f}", str(out)]
    return run_ffmpeg(cmd, what=f"rendering video -> {out.name}")


# -------------------------------------------------------------- chat/player

TIME_KEYS = ("time", "ts", "timestamp", "start", "starttime", "start-time", "offset", "at")


def parse_events(xml_files: list[Path]) -> tuple[list[dict], list[dict]]:
    """Pull chapter markers and chat lines out of the recording's XML.

    The schemas vary between Connect versions and are undocumented, so this is
    deliberately tolerant: anything with a time-like attribute and something
    that reads as text is taken, and everything else is ignored.
    """
    markers: list[dict] = []
    chat: list[dict] = []
    for path in xml_files:
        try:
            tree = ET.parse(path)
        except (ET.ParseError, OSError):
            continue
        is_chat = "chat" in path.name.lower()
        for element in tree.iter():
            time_ms = None
            for key, value in element.attrib.items():
                if key.lower().replace("_", "-") in TIME_KEYS:
                    try:
                        time_ms = int(float(value))
                    except (TypeError, ValueError):
                        time_ms = None
                    break
            if time_ms is None or time_ms < 0:
                continue
            text = _text_of(element, ("message", "text", "body", "value", "content")
                            if is_chat else ("name", "label", "title", "text", "value"))
            if not text:
                continue
            if is_chat:
                sender = _text_of(element, ("from", "sender", "user", "username", "author"))
                chat.append({"t": time_ms / 1000.0, "from": sender or "", "msg": text})
            elif len(text) <= 160 and not text.replace(".", "").isdigit():
                markers.append({"t": time_ms / 1000.0, "label": text})
    markers.sort(key=lambda m: m["t"])
    chat.sort(key=lambda c: c["t"])
    return markers, chat


def _text_of(element: ET.Element, keys) -> str:
    for key in keys:
        for attr, value in element.attrib.items():
            if attr.lower().replace("_", "-") == key and value.strip():
                return _clean(value)
        child = element.find(key)
        if child is not None and child.text and child.text.strip():
            return _clean(child.text)
    return _clean(element.text) if element.text else ""


def _clean(text: str) -> str:
    text = html_mod.unescape(text)
    text = re.sub(r"<[^>]+>", "", text)     # Connect wraps chat in markup
    return re.sub(r"\s+", " ", text).strip()


PLAYER = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>__TITLE__</title>
<style>
*{box-sizing:border-box}html,body{height:100%;margin:0}
body{background:#14161a;color:#e7e9ee;font:15px/1.5 system-ui,sans-serif;display:flex;flex-direction:column}
header{padding:10px 16px;border-bottom:1px solid #2c313b;display:flex;gap:12px;align-items:baseline;flex-wrap:wrap}
h1{font-size:16px;margin:0}main{flex:1;display:flex;min-height:0}
.stage{flex:1;min-width:0;display:flex;flex-direction:column;background:#000}
video,audio{width:100%;flex:1;min-height:0;background:#000}
audio{height:54px;flex:0 0 auto;margin:auto;max-width:640px}
.bar{background:#1c1f26;border-top:1px solid #2c313b;padding:10px 14px;display:flex;gap:12px;align-items:center;flex-wrap:wrap}
button{background:#262b35;color:#e7e9ee;border:1px solid #2c313b;border-radius:6px;padding:5px 10px;cursor:pointer;font:inherit;font-size:13px}
button.on{background:#5b9dff;color:#08101f;font-weight:600}
.clock{color:#99a0ae;font-variant-numeric:tabular-nums;font-size:13px}
aside{width:330px;flex:0 0 330px;border-left:1px solid #2c313b;background:#1c1f26;display:flex;flex-direction:column;min-height:0}
.tabs{display:flex;border-bottom:1px solid #2c313b}
.tabs button{flex:1;border:0;border-bottom:2px solid transparent;background:none;color:#99a0ae;border-radius:0}
.tabs button.on{background:none;color:#e7e9ee;border-bottom-color:#5b9dff}
.list{overflow-y:auto;flex:1;padding:6px}
.row{display:flex;gap:9px;padding:7px 8px;border-radius:6px;cursor:pointer}
.row:hover{background:#262b35}.row.now{background:#22314b}
.t{color:#5b9dff;font-size:12px;font-variant-numeric:tabular-nums;flex:0 0 auto}
.who{color:#99a0ae;font-size:12px}.txt{min-width:0;overflow-wrap:anywhere}
.hint{padding:8px 14px;border-top:1px solid #2c313b;color:#99a0ae;font-size:12px}
@media(max-width:820px){main{flex-direction:column}aside{width:auto;flex:0 0 45%;border-left:0;border-top:1px solid #2c313b}}
</style></head><body>
<header><h1 id="title"></h1><span class="clock" id="meta"></span></header>
<main><div class="stage"><div id="media"></div>
<div class="bar"><button id="play">Play</button>
<button data-seek="-30">&#171;30s</button><button data-seek="-10">&#171;10s</button>
<button data-seek="10">10s&#187;</button><button data-seek="30">30s&#187;</button>
<span class="clock" id="clock">0:00 / 0:00</span><span id="speeds"></span></div></div>
<aside><div class="tabs"><button data-tab="chapters" class="on">Chapters</button>
<button data-tab="chat">Chat</button></div>
<div class="list" id="chapters"></div><div class="list" id="chat" hidden></div>
<div class="hint">space play &middot; &larr;&rarr; 10s &middot; J/L 30s &middot; &uarr;&darr; speed &middot; 0 reset</div>
</aside></main>
<script>
const DATA=/*__DATA__*/null, $=(i)=>document.getElementById(i);
const fmt=(s)=>{if(!isFinite(s)||s<0)s=0;const h=Math.floor(s/3600),m=Math.floor(s%3600/60),x=Math.floor(s%60);
return (h?h+":"+String(m).padStart(2,"0"):String(m))+":"+String(x).padStart(2,"0");};
document.title=DATA.title;$("title").textContent=DATA.title;
let media;
if(DATA.video){media=document.createElement("video");media.src=DATA.video;}
else{media=document.createElement("audio");media.src=DATA.audio;}
media.controls=true;media.preload="metadata";$("media").replaceWith(media);
$("meta").textContent=(DATA.markers.length?DATA.markers.length+" chapters · ":"")+
  (DATA.chat.length?DATA.chat.length+" chat lines · ":"")+fmt(DATA.duration);
const STORE="ac_downloader:"+DATA.key, saved=JSON.parse(localStorage.getItem(STORE)||"{}");
let rate=saved.rate||1;
function applyRate(r){rate=Math.min(16,Math.max(0.25,Math.round(r*100)/100));
  media.playbackRate=rate;media.preservesPitch=true;media.mozPreservesPitch=true;media.webkitPreservesPitch=true;
  for(const b of $("speeds").children)b.classList.toggle("on",Number(b.dataset.r)===rate);save();}
for(const s of [0.5,0.75,1,1.25,1.5,2,2.5,3,4,6,8,12,16]){
  const b=document.createElement("button");b.textContent=s+"x";b.dataset.r=s;b.onclick=()=>applyRate(s);
  $("speeds").appendChild(b);}
const dur=()=>isFinite(media.duration)&&media.duration>0?media.duration:DATA.duration;
const seekTo=(t)=>{media.currentTime=Math.max(0,Math.min(dur()-0.25,t));};
$("play").onclick=()=>media.paused?media.play():media.pause();
media.addEventListener("play",()=>{$("play").textContent="Pause";follow();});
media.addEventListener("pause",()=>$("play").textContent="Play");
for(const b of document.querySelectorAll("[data-seek]"))b.onclick=()=>seekTo(media.currentTime+Number(b.dataset.seek));
let t=null;function save(){clearTimeout(t);t=setTimeout(()=>localStorage.setItem(STORE,
  JSON.stringify({rate:rate,time:media.currentTime})),400);}
media.addEventListener("loadedmetadata",()=>{applyRate(rate);
  if(saved.time&&saved.time<dur()-5)media.currentTime=saved.time;tick();});
const esc=(s)=>String(s==null?"":s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
function fill(box,items,render){if(!items.length){box.innerHTML='<div class="hint">Nothing recovered.</div>';return[];}
  return items.map(it=>{const r=document.createElement("div");r.className="row";r.innerHTML=render(it);
    r.onclick=()=>{seekTo(it.t);media.play();};box.appendChild(r);return r;});}
const chapRows=fill($("chapters"),DATA.markers,m=>'<span class="t">'+fmt(m.t)+'</span><span class="txt">'+esc(m.label)+'</span>');
const chatRows=fill($("chat"),DATA.chat,c=>'<span class="t">'+fmt(c.t)+'</span><span class="txt">'+
  (c.from?'<span class="who">'+esc(c.from)+'</span> ':'')+esc(c.msg)+'</span>');
for(const b of document.querySelectorAll("[data-tab]"))b.onclick=()=>{
  for(const o of document.querySelectorAll("[data-tab]"))o.classList.toggle("on",o===b);
  $("chapters").hidden=b.dataset.tab!=="chapters";$("chat").hidden=b.dataset.tab!=="chat";};
// Everything below reads media.currentTime and nothing else, so the sidebar
// cannot drift away from the picture however fast it is playing.
function hi(rows,items){let a=-1;for(let i=0;i<items.length;i++)if(items[i].t<=media.currentTime+0.25)a=i;
  rows.forEach((r,i)=>r.classList.toggle("now",i===a));return a;}
let lastChat=-1;
function tick(){$("clock").textContent=fmt(media.currentTime)+" / "+fmt(dur());
  hi(chapRows,DATA.markers);const i=hi(chatRows,DATA.chat);
  if(i!==lastChat){lastChat=i;if(i>=0&&!$("chat").hidden)chatRows[i].scrollIntoView({block:"nearest"});}save();}
let following=false;
function follow(){if(following)return;following=true;
  const step=()=>{if(media.paused||media.ended){following=false;tick();return;}tick();requestAnimationFrame(step);};
  requestAnimationFrame(step);}
media.addEventListener("timeupdate",()=>{if(media.paused)tick();});
media.addEventListener("seeked",tick);
addEventListener("keydown",e=>{if(e.target.tagName==="INPUT"||e.ctrlKey||e.metaKey||e.altKey)return;
  switch(e.key){case " ":case "k":media.paused?media.play():media.pause();break;
  case "ArrowLeft":seekTo(media.currentTime-10);break;case "ArrowRight":seekTo(media.currentTime+10);break;
  case "j":seekTo(media.currentTime-30);break;case "l":seekTo(media.currentTime+30);break;
  case "ArrowUp":applyRate(rate+0.25);break;case "ArrowDown":applyRate(rate-0.25);break;
  case "0":applyRate(1);break;default:return;}e.preventDefault();});
</script></body></html>
"""


def write_player(out: Path, *, title: str, video: str | None, audio: str | None,
                 duration_s: float, markers: list[dict], chat: list[dict]) -> Path:
    payload = {"title": title, "video": video, "audio": audio,
               "duration": duration_s, "markers": markers, "chat": chat,
               "key": out.stem}
    page = PLAYER.replace("/*__DATA__*/null", json.dumps(payload, ensure_ascii=False))
    page = page.replace("__TITLE__", title.replace("&", "&amp;").replace("<", "&lt;")
                        .replace(">", "&gt;").replace('"', "&quot;"))
    out.write_text(page, encoding="utf-8")
    return out


# --------------------------------------------------------------------- main

def require_tools() -> None:
    missing = [t for t in ("ffmpeg", "ffprobe") if shutil.which(t) is None]
    if missing:
        die(f"{' and '.join(missing)} not found on PATH.\n"
            "  macOS         : brew install ffmpeg\n"
            "  Debian/Ubuntu : sudo apt install ffmpeg\n"
            "  Fedora        : sudo dnf install ffmpeg")


def pick_audio(streams: list[Stream], only_speaker: int | None = None) -> list[Stream]:
    """The microphones, in preference to the server's re-encoded composite."""
    cams = [s for s in streams if s.role == "camera" and s.has_audio]
    chosen = cams or [s for s in streams if s.has_audio]
    if only_speaker is not None:
        chosen = [s for s in chosen if s.speaker == only_speaker]
    return sorted(chosen, key=lambda s: (s.offset_ms, s.seq))


def pick_video(streams: list[Stream]) -> tuple[list[Stream], list[Stream]]:
    """(main picture, picture-in-picture)."""
    screens = [s for s in streams if s.role == "screen" and s.has_video]
    if not screens:
        screens = [s for s in streams if s.role == "main" and s.has_video]
    cams = [s for s in streams if s.role == "camera" and s.has_video]
    if not screens:                       # the camera is all there is
        return sorted(cams, key=lambda s: s.offset_ms), []
    return (sorted(screens, key=lambda s: s.offset_ms),
            sorted(cams, key=lambda s: s.offset_ms))


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Download an Adobe Connect recording and rebuild it as "
                    "playable audio and video. Every option is optional.")
    ap.add_argument("url", nargs="?",
                    help="any Connect link; a ?session= token in it is used "
                         "automatically. Asked for if omitted.")
    ap.add_argument("--session", help="BREEZESESSION cookie value (overrides one in the URL)")
    ap.add_argument("--out", default="./recording", help="output directory")
    ap.add_argument("--speed", type=float, default=1.0,
                    help="also write a permanently sped-up copy of the audio, e.g. 2")
    ap.add_argument("--layout", choices=("auto", "timeline", "sequential"), default="auto",
                    help="how to place segments in time (default: auto)")
    ap.add_argument("--only-speaker", type=int, metavar="N",
                    help="keep just one speaker's microphone (see --inspect)")
    ap.add_argument("--no-level", action="store_true",
                    help="do not equalise the loudness of different microphones")
    ap.add_argument("--audio-only", action="store_true", help="skip the video")
    ap.add_argument("--no-open", action="store_true",
                    help="do not open the player in a browser when finished")
    ap.add_argument("--keep-zip", action="store_true", help="keep the downloaded archive")
    ap.add_argument("--inspect", action="store_true",
                    help="download and report the timeline, then stop")
    ap.add_argument("--version", action="version", version=f"ac_downloader {VERSION}")
    args = ap.parse_args()

    require_tools()

    raw = args.url or (input("Connect link: ").strip() if sys.stdin.isatty() else "")
    if not raw:
        die("no link given")
    try:
        link = Link(raw)
    except ValueError as exc:
        die(str(exc))

    session = args.session or link.session
    if session and not args.session:
        print("[*] using the session token found in the link")
    if not session and sys.stdin.isatty():
        session = getpass.getpass("BREEZESESSION (blank if public): ").strip() or None

    print(f"[*] server : {link.origin}")
    print(f"[*] handle : {link.handle}")

    out_dir = Path(args.out).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)

    zip_path = fetch_archive(link, session, out_dir)
    streams, xml_files = unpack(zip_path, out_dir / "streams")
    mode = resolve_offsets(streams, layout=args.layout)

    quiet: list[Stream] = []
    if not args.no_level:
        print("[*] measuring microphone levels")
        quiet = level_streams(streams)

    print()
    describe_layout(mode)
    print("\n[*] timeline")
    for s in sorted(streams, key=lambda x: (x.offset_ms, x.seq)):
        print("   ", s)

    totals: dict[int, int] = {}
    for s in streams:
        if s.has_audio:
            totals[s.speaker] = totals.get(s.speaker, 0) + s.duration_ms
    if len(totals) > 1:
        print("\n[*] speakers, by microphone time")
        for speaker, ms in sorted(totals.items(), key=lambda kv: -kv[1]):
            print(f"    speaker {speaker}: {ms / 60000:5.1f} min")
        print("    the lecturer is usually the one with the most;")
        print("    keep only them with --only-speaker N")

    if quiet:
        print("\n[*] quiet microphones raised so they can be heard")
        for s in sorted(quiet, key=lambda x: -x.gain_db):
            print(f"    {s.name:<28} {s.mean_db:6.1f}dB  ->  +{s.gain_db:.0f}dB")
        print("    without this these voices are buried under the main mic;")
        print("    use --no-level to keep the original levels")

    print(f"\n    total length: {max(s.end_ms for s in streams) / 60000:.1f} min")
    if args.inspect:
        return 0

    audio = pick_audio(streams, args.only_speaker)
    if args.only_speaker is not None and not audio:
        die(f"no audio found for speaker {args.only_speaker}")

    audio_name = video_name = None
    if audio:
        print()
        if render_audio(audio, out_dir / "lecture.mp3"):
            audio_name = "lecture.mp3"
        if audio_name and abs(args.speed - 1.0) > 1e-6:
            render_audio(audio, out_dir / f"lecture_{args.speed:g}x.mp3", speed=args.speed)
    else:
        print("\n[!] no audio stream found - the class may have used a telephone "
              "bridge, whose audio is not in the archive")

    if not args.audio_only:
        screens, cams = pick_video(streams)
        if screens:
            if render_video(screens, cams, audio, out_dir / "lecture.mp4"):
                video_name = "lecture.mp4"
        else:
            print("[*] no picture in this recording; audio only")

    markers, chat = parse_events(xml_files)
    if chat:
        (out_dir / "chat.txt").write_text(
            "\n".join(f"[{ts(int(c['t'] * 1000)).strip()}] "
                      f"{c['from'] + ': ' if c['from'] else ''}{c['msg']}" for c in chat) + "\n",
            encoding="utf-8")

    page = None
    if audio_name or video_name:
        page = write_player(out_dir / "play.html", title=link.handle or "lecture",
                            video=video_name, audio=audio_name,
                            duration_s=max(s.end_ms for s in streams) / 1000.0,
                            markers=markers, chat=chat)

    if not args.keep_zip:
        zip_path.unlink(missing_ok=True)

    print(f"\n[+] done - {out_dir}")
    for f in sorted(out_dir.glob("*")):
        if f.is_file():
            print(f"    {f.name}  ({f.stat().st_size >> 20} MiB)")

    if page and not args.no_open and not os.environ.get("CONNECT_DL_NO_BROWSER"):
        try:
            webbrowser.open(page.resolve().as_uri())
        except (webbrowser.Error, OSError, ValueError):
            print(f"\n    open {page} in a browser to watch it")
    return 0 if (audio_name or not audio) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\ninterrupted")
        sys.exit(130)

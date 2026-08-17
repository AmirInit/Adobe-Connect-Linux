"""Making sense of the pile of files inside a recording archive.

A Flash-era Adobe Connect recording unzips to something like::

    mainstream.flv / mainstream.xml      the room's composited "main" stream
    indexstream.flv / indexstream.xml    seek index and pod layout events
    cameraVoip_1_1.flv                   webcam picture + the microphone audio
    cameraVoip_1_2.flv                   ...one per start/stop of the mic
    screenshare_1_1.flv / .flx           whatever was being shared on screen
    ftchat.xml, sco_metadata.xml, *.swf  chat log, titles, player chrome

The hard part is that each FLV is an independent stream that began at a
different moment in the meeting, and nothing in the file names says when.  Put
them on a common timeline wrong and the professor's voice drifts away from the
slides - or worse, several microphone segments pile up on top of each other.

Offsets are resolved in descending order of trustworthiness:

1. a start time stated for that file in one of the XML sidecars,
2. the file's own first media timestamp (Connect frequently leaves these as
   meeting-relative rather than resetting them to zero),
3. laying the segments end to end in file-name order.

Which rule fired is recorded per stream and printed, because rule 3 is a guess
and the user may need to correct it with --offset.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from .flv import FlvInfo, scan_flv

log = logging.getLogger(__name__)

__all__ = ["Role", "Stream", "RecordingArchive", "unpack"]

_SEQ_RE = re.compile(r"(\d+)")
_TIME_ATTRS = ("start", "starttime", "start-time", "begin", "offset", "time", "ts", "timestamp")
_AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".wav"}


class Role(str, Enum):
    SCREENSHARE = "screenshare"   # shared screen / slides - the main picture
    CAMERA = "camera"             # webcam video, usually carries the mic audio
    VOICE = "voice"               # audio-only stream (VoIP or telephony bridge)
    MAIN = "main"                 # mainstream.flv, the server's own composite
    INDEX = "index"               # indexstream.flv, events not media
    OTHER = "other"

    @property
    def is_audio_source(self) -> bool:
        return self in (Role.CAMERA, Role.VOICE, Role.MAIN)


@dataclass
class Stream:
    path: Path
    role: Role
    info: FlvInfo | None = None
    offset_ms: int = 0
    offset_source: str = "unresolved"

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def duration_ms(self) -> int:
        return self.info.duration_ms if self.info else 0

    @property
    def end_ms(self) -> int:
        return self.offset_ms + self.duration_ms

    @property
    def has_audio(self) -> bool:
        if self.info:
            return self.info.has_audio
        return self.path.suffix.lower() in _AUDIO_EXTS

    @property
    def has_video(self) -> bool:
        return bool(self.info and self.info.has_video)

    @property
    def sequence(self) -> tuple[int, ...]:
        """Numbers embedded in the file name, for ordering segments."""
        return tuple(int(n) for n in _SEQ_RE.findall(self.path.stem)) or (0,)

    def describe(self) -> str:
        detail = self.info.describe() if self.info else "(non-FLV)"
        return (
            f"{self.name:<28} {self.role.value:<11} @{self.offset_ms / 1000:8.1f}s  "
            f"{detail}  [{self.offset_source}]"
        )


@dataclass
class RecordingArchive:
    root: Path
    streams: list[Stream] = field(default_factory=list)
    xml_files: list[Path] = field(default_factory=list)
    title: str | None = None

    # ------------------------------------------------------------ selection

    def by_role(self, *roles: Role) -> list[Stream]:
        wanted = set(roles)
        return sorted(
            (s for s in self.streams if s.role in wanted),
            key=lambda s: (s.offset_ms, s.sequence),
        )

    @property
    def audio_streams(self) -> list[Stream]:
        """Everything that could carry the speaker's voice, best sources first.

        Camera/VoIP segments are preferred; ``mainstream`` is only used when
        there is nothing else, because it is a re-encode of the same audio.
        """
        primary = [s for s in self.by_role(Role.CAMERA, Role.VOICE) if s.has_audio]
        if primary:
            return primary
        return [s for s in self.by_role(Role.MAIN) if s.has_audio]

    @property
    def video_streams(self) -> list[Stream]:
        primary = [s for s in self.by_role(Role.SCREENSHARE) if s.has_video]
        if primary:
            return primary
        return [s for s in self.by_role(Role.MAIN) if s.has_video]

    @property
    def camera_streams(self) -> list[Stream]:
        return [s for s in self.by_role(Role.CAMERA) if s.has_video]

    @property
    def duration_ms(self) -> int:
        return max((s.end_ms for s in self.streams if s.role != Role.INDEX), default=0)

    def summary(self) -> str:
        lines = [f"archive: {self.root}"]
        if self.title:
            lines.append(f"title:   {self.title}")
        lines.append(f"length:  {self.duration_ms / 1000 / 60:.1f} min")
        lines.append("streams:")
        lines += ["  " + s.describe() for s in sorted(self.streams, key=lambda s: (s.role.value, s.sequence))]
        if not self.audio_streams:
            lines.append("  !! no audio-bearing stream found")
        return "\n".join(lines)


def unpack(payload: Path, dest: Path) -> RecordingArchive:
    """Analyse a recording, extracting it first if it is still a zip.

    ``payload`` may be a zip file, an already-unpacked directory, or a loose
    media file; ``dest`` is only used in the zip case.
    """
    if payload.is_dir():
        root = payload
    elif zipfile.is_zipfile(payload):
        dest.mkdir(parents=True, exist_ok=True)
        _safe_extract(payload, dest)
        root = dest
    else:
        root = payload.parent

    archive = RecordingArchive(root=root)
    _collect(archive, root)
    _resolve_offsets(archive)
    archive.title = _find_title(archive)
    return archive


def _safe_extract(zip_path: Path, dest: Path) -> None:
    """Extract, refusing entries that would escape the destination."""
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.infolist():
            target = (dest / member.filename).resolve()
            if not str(target).startswith(str(dest.resolve())):
                raise ValueError(f"refusing unsafe archive entry {member.filename!r}")
        zf.extractall(dest)
    log.info("extracted %s", zip_path.name)


def _collect(archive: RecordingArchive, root: Path) -> None:
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        if suffix == ".xml":
            archive.xml_files.append(path)
            continue
        if suffix == ".flv":
            info = scan_flv(path)
            role = _classify(path, info)
            if info.error:
                log.warning("%s: %s", path.name, info.error)
            archive.streams.append(Stream(path=path, role=role, info=info))
        elif suffix in _AUDIO_EXTS:
            archive.streams.append(Stream(path=path, role=Role.VOICE))


def _classify(path: Path, info: FlvInfo) -> Role:
    stem = path.stem.lower()
    if stem.startswith("screenshare") or "screen" in stem:
        return Role.SCREENSHARE
    if "cameravoip" in stem or stem.startswith("camera"):
        return Role.CAMERA
    if "voice" in stem or "voip" in stem or "telephony" in stem or "audio" in stem:
        return Role.VOICE
    if stem.startswith("indexstream"):
        return Role.INDEX
    if stem.startswith("mainstream"):
        return Role.MAIN
    # Unknown name: fall back to what the container actually holds.
    if info.has_video:
        return Role.SCREENSHARE if (info.width or 0) >= 800 else Role.CAMERA
    if info.has_audio:
        return Role.VOICE
    return Role.OTHER


# ------------------------------------------------------------------ offsets

def _resolve_offsets(archive: RecordingArchive) -> None:
    hints = _offset_hints(archive.xml_files)

    # Connect writes FLV timestamps one of two ways, and which one is in use
    # decides how a segment starting at t=0 should be read.
    #
    #   meeting-relative : every segment is stamped with its position in the
    #                      meeting, so 0 genuinely means "from the start".
    #   per-segment      : every segment restarts at 0, so a 0 says nothing at
    #                      all and the segments have to be laid end to end.
    #
    # A single non-zero start anywhere in the recording settles it: only the
    # first style can produce one.  Getting this wrong is not a subtle
    # mis-sync - it reorders halves of the lecture - so it is decided once for
    # the whole archive rather than per file.
    media = [s for s in archive.streams if s.info and s.info.ok]
    meeting_relative = any(s.info.first_media_ts > 0 for s in media)
    log.info(
        "stream timestamps look %s",
        "meeting-relative" if meeting_relative else "per-segment (each restarts at zero)",
    )

    for stream in archive.streams:
        key = stream.path.stem.lower()
        if key in hints:
            stream.offset_ms = hints[key]
            stream.offset_source = "xml"
        elif meeting_relative and stream.info and stream.info.ok:
            stream.offset_ms = stream.info.first_media_ts
            stream.offset_source = "flv-timestamp"

    # Anything still unplaced gets laid end to end within its own role, so that
    # consecutive microphone segments do not stack on top of each other.
    for role in Role:
        pending = [s for s in archive.streams if s.role == role and s.offset_source == "unresolved"]
        if not pending:
            continue
        placed = [s for s in archive.streams if s.role == role and s.offset_source != "unresolved"]
        cursor = max((s.end_ms for s in placed), default=0)
        for stream in sorted(pending, key=lambda s: s.sequence):
            stream.offset_ms = cursor
            stream.offset_source = "sequential" if cursor else "assumed-zero"
            cursor += stream.duration_ms

    _normalise(archive)


def _normalise(archive: RecordingArchive) -> None:
    """Shift the whole timeline so the recording starts at zero."""
    considered = [s for s in archive.streams if s.role != Role.INDEX and s.duration_ms > 0]
    if not considered:
        return
    base = min(s.offset_ms for s in considered)
    if base <= 0:
        return
    for stream in archive.streams:
        stream.offset_ms = max(0, stream.offset_ms - base)
    log.info("shifted timeline back by %.1fs so the recording starts at zero", base / 1000)


def _offset_hints(xml_files: list[Path]) -> dict[str, int]:
    """Scan the XML sidecars for 'stream X starts at T' statements.

    The schemas differ between Connect versions, so rather than target one
    layout we look for any element that names an FLV and also carries a
    time-like attribute.
    """
    hints: dict[str, int] = {}
    for xml_path in xml_files:
        try:
            tree = ET.parse(xml_path)
        except (ET.ParseError, OSError):
            continue
        for element in tree.iter():
            names = _referenced_streams(element)
            if not names:
                continue
            time_ms = _time_from(element)
            if time_ms is None:
                continue
            for name in names:
                hints.setdefault(name, time_ms)
    if hints:
        log.info("found start times for %d stream(s) in the recording XML", len(hints))
    return hints


def _referenced_streams(element: ET.Element) -> list[str]:
    """FLV base names mentioned by this element's attributes or text."""
    found = []
    candidates = list(element.attrib.values())
    if element.text:
        candidates.append(element.text)
    for value in candidates:
        value = (value or "").strip()
        if not value or "/" in value and len(value) > 120:
            continue
        stem = value.rsplit("/", 1)[-1]
        if stem.lower().endswith(".flv"):
            found.append(stem[:-4].lower())
        elif re.fullmatch(r"(cameraVoip|screenshare|mainstream|indexstream)[\w.-]*", stem, re.I):
            found.append(stem.lower())
    return found


def _time_from(element: ET.Element) -> int | None:
    for attr in _TIME_ATTRS:
        for key, value in element.attrib.items():
            if key.lower().replace("_", "-") != attr:
                continue
            parsed = _to_ms(value)
            if parsed is not None:
                return parsed
    return None


def _to_ms(value: str) -> int | None:
    """Read a time value, guessing between seconds and milliseconds."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number < 0:
        return None
    # Connect writes stream times in milliseconds.  A bare small number is
    # ambiguous, but treating "12" as 12ms rather than 12s is the safer error:
    # it collapses to roughly zero instead of shifting audio by seconds.
    return int(number)


def _find_title(archive: RecordingArchive) -> str | None:
    for xml_path in archive.xml_files:
        if "metadata" not in xml_path.name.lower():
            continue
        try:
            tree = ET.parse(xml_path)
        except (ET.ParseError, OSError):
            continue
        for tag in ("name", "title", "displayName"):
            node = tree.find(f".//{tag}")
            if node is not None and node.text and node.text.strip():
                return node.text.strip()
    return None

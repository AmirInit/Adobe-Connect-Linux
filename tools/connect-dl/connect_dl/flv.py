"""A read-only FLV inspector.

Adobe Connect recordings are delivered as a pile of FLV files with no manifest
that a normal media tool understands.  Before anything can be re-assembled we
need, per file: which codecs are inside, whether there is audio at all, and the
span of timestamps it covers.  ffprobe can answer some of that, but Connect's
FLVs routinely have a broken or absent duration in their metadata, and we want
the tool to work even before ffmpeg is installed - so this parses the container
directly.

The scan only reads tag headers and seeks past payloads, so it costs a few
hundred seeks even on a multi-gigabyte file.

Format reference: FLV is an 9-byte header, then a chain of
``(4-byte previous tag size, 11-byte tag header, payload)``.
"""

from __future__ import annotations

import logging
import struct
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

__all__ = [
    "FlvInfo", "scan_flv", "iter_script_tags", "AUDIO_CODECS", "VIDEO_CODECS",
]

TAG_AUDIO = 8
TAG_VIDEO = 9
TAG_SCRIPT = 18

AUDIO_CODECS = {
    0: "pcm", 1: "adpcm", 2: "mp3", 3: "pcm_le",
    4: "nellymoser_16k", 5: "nellymoser_8k", 6: "nellymoser",
    7: "alaw", 8: "mulaw", 10: "aac", 11: "speex",
    14: "mp3_8k", 15: "device",
}

VIDEO_CODECS = {
    2: "h263", 3: "flashsv", 4: "vp6", 5: "vp6a", 6: "flashsv2", 7: "h264",
}

# Codecs no browser will play; anything using them has to be transcoded before
# it can go into the HTML player.
LEGACY_AUDIO = {"nellymoser", "nellymoser_8k", "nellymoser_16k", "speex", "adpcm", "alaw", "mulaw"}
LEGACY_VIDEO = {"h263", "flashsv", "flashsv2", "vp6", "vp6a"}


@dataclass
class FlvInfo:
    path: Path
    ok: bool = False
    error: str | None = None

    has_audio: bool = False
    has_video: bool = False
    audio_codec: str | None = None
    video_codec: str | None = None

    first_ts: int = 0          # milliseconds, first tag of any kind
    first_media_ts: int = 0    # milliseconds, first audio/video tag
    last_ts: int = 0           # milliseconds
    audio_tags: int = 0
    video_tags: int = 0

    width: int | None = None
    height: int | None = None
    metadata: dict = field(default_factory=dict)

    @property
    def duration_ms(self) -> int:
        """Span covered by real media tags."""
        declared = self.metadata.get("duration")
        if isinstance(declared, (int, float)) and declared > 0:
            return int(declared * 1000)
        return max(0, self.last_ts - self.first_media_ts)

    @property
    def duration_s(self) -> float:
        return self.duration_ms / 1000.0

    @property
    def needs_transcode(self) -> bool:
        return (self.audio_codec in LEGACY_AUDIO) or (self.video_codec in LEGACY_VIDEO)

    def describe(self) -> str:
        parts = []
        if self.has_video:
            size = f" {self.width}x{self.height}" if self.width else ""
            parts.append(f"video={self.video_codec}{size}")
        if self.has_audio:
            parts.append(f"audio={self.audio_codec}")
        if not parts:
            parts.append("no media tags")
        return f"{', '.join(parts)}, {self.duration_s:.1f}s"


def scan_flv(path: Path, *, max_tags: int | None = None) -> FlvInfo:
    """Inspect an FLV file.  Never raises for malformed input."""
    info = FlvInfo(path=path)
    try:
        size = path.stat().st_size
    except OSError as exc:
        info.error = str(exc)
        return info

    try:
        with open(path, "rb") as fh:
            header = fh.read(9)
            if len(header) < 9 or header[:3] != b"FLV":
                info.error = "not an FLV file"
                return info

            header_size = struct.unpack(">I", header[5:9])[0]
            fh.seek(max(header_size, 9))
            fh.read(4)  # PreviousTagSize0

            first_seen = False
            count = 0
            while True:
                tag_header = fh.read(11)
                if len(tag_header) < 11:
                    break

                tag_type = tag_header[0] & 0x1F
                data_size = int.from_bytes(tag_header[1:4], "big")
                ts = int.from_bytes(tag_header[4:7], "big") | (tag_header[7] << 24)

                if data_size < 0 or fh.tell() + data_size > size:
                    break

                if not first_seen:
                    info.first_ts = ts
                    first_seen = True

                if tag_type == TAG_SCRIPT:
                    payload = fh.read(data_size)
                    meta = _parse_script_tag(payload)
                    if meta:
                        info.metadata.update(meta)
                elif tag_type == TAG_AUDIO and data_size >= 1:
                    first_byte = fh.read(1)[0]
                    fh.seek(data_size - 1, 1)
                    if not info.has_audio:
                        info.has_audio = True
                        info.audio_codec = AUDIO_CODECS.get(first_byte >> 4, f"unknown({first_byte >> 4})")
                        info.first_media_ts = _min_media(info, ts)
                    info.audio_tags += 1
                    info.last_ts = max(info.last_ts, ts)
                elif tag_type == TAG_VIDEO and data_size >= 1:
                    first_byte = fh.read(1)[0]
                    fh.seek(data_size - 1, 1)
                    if not info.has_video:
                        info.has_video = True
                        info.video_codec = VIDEO_CODECS.get(first_byte & 0x0F, f"unknown({first_byte & 0x0F})")
                        info.first_media_ts = _min_media(info, ts)
                    info.video_tags += 1
                    info.last_ts = max(info.last_ts, ts)
                else:
                    fh.seek(data_size, 1)

                fh.read(4)  # PreviousTagSize
                count += 1
                if max_tags and count >= max_tags:
                    break

        info.width = _as_int(info.metadata.get("width"))
        info.height = _as_int(info.metadata.get("height"))
        info.ok = info.has_audio or info.has_video
        if not info.ok and not info.error:
            info.error = "container parsed but contains no audio or video tags"
    except (OSError, struct.error) as exc:  # pragma: no cover - defensive
        info.error = f"{type(exc).__name__}: {exc}"

    return info


def _min_media(info: FlvInfo, ts: int) -> int:
    if info.audio_tags == 0 and info.video_tags == 0:
        return ts
    return min(info.first_media_ts, ts)


def _as_int(value) -> int | None:
    if isinstance(value, (int, float)) and value > 0:
        return int(value)
    return None


def iter_script_tags(path: Path, *, max_tags: int = 200_000):
    """Yield ``(timestamp_ms, name, value, undecoded_marker, raw_len)`` per tag.

    Connect's event streams (``ftchat``, ``ftcontent``, ``indexstream``…) are
    FLV containers whose payload is entirely script-data tags: AMF0 messages
    with a name and an argument, timestamped against the meeting.  Nothing here
    interprets them - it is a faithful decode of whatever is in the file, so
    that a stream can be *reported* even when its vocabulary is unknown.

    ``value`` is ``None`` when the AMF0 body could not be decoded; ``raw_len``
    is always the payload size, so undecodable tags can still be counted and
    measured.
    """
    try:
        size = path.stat().st_size
        with open(path, "rb") as fh:
            header = fh.read(9)
            if len(header) < 9 or header[:3] != b"FLV":
                return
            fh.seek(max(struct.unpack(">I", header[5:9])[0], 9))
            fh.read(4)  # PreviousTagSize0

            for _ in range(max_tags):
                tag_header = fh.read(11)
                if len(tag_header) < 11:
                    return
                tag_type = tag_header[0] & 0x1F
                data_size = int.from_bytes(tag_header[1:4], "big")
                ts = int.from_bytes(tag_header[4:7], "big") | (tag_header[7] << 24)
                if data_size < 0 or fh.tell() + data_size > size:
                    return

                if tag_type == TAG_SCRIPT:
                    payload = fh.read(data_size)
                    yield (ts,) + _script_message(payload) + (data_size,)
                else:
                    fh.seek(data_size, 1)
                fh.read(4)  # PreviousTagSize
    except (OSError, struct.error) as exc:  # pragma: no cover - defensive
        log.debug("%s: %s", path, exc)
        return


def _script_message(payload: bytes) -> tuple[str | None, object, int | None]:
    """Decode one script tag into ``(name, value, undecoded_marker)``.

    Tolerates anything.  ``undecoded_marker`` is the AMF type byte that could
    not be read, or ``None`` when the body decoded - worth keeping, because
    marker 0x11 means the body switched to AMF3 and that is the single most
    useful thing to know about a stream we cannot read.
    """
    try:
        name, offset = _amf0_value(payload, 0)
    except (ValueError, struct.error, UnicodeDecodeError, IndexError):
        return None, None, payload[0] if payload else None
    if not isinstance(name, str):
        return None, None, payload[0] if payload else None
    try:
        value, _ = _amf0_value(payload, offset)
    except (ValueError, struct.error, UnicodeDecodeError, IndexError):
        return name, None, payload[offset] if offset < len(payload) else None
    return name, value, None


# ------------------------------------------------------------------ AMF0

def _parse_script_tag(payload: bytes) -> dict:
    """Pull the property map out of an ``onMetaData`` script tag."""
    try:
        name, offset = _amf0_value(payload, 0)
        if name != "onMetaData":
            return {}
        value, _ = _amf0_value(payload, offset)
        return value if isinstance(value, dict) else {}
    except (ValueError, struct.error, UnicodeDecodeError, IndexError):
        return {}


def _amf0_value(buf: bytes, i: int):
    marker = buf[i]
    i += 1
    if marker == 0x00:  # number
        return struct.unpack_from(">d", buf, i)[0], i + 8
    if marker == 0x01:  # boolean
        return bool(buf[i]), i + 1
    if marker == 0x02:  # short string
        return _amf0_string(buf, i)
    if marker == 0x03:  # object
        return _amf0_props(buf, i)
    if marker in (0x05, 0x06):  # null / undefined
        return None, i
    if marker == 0x08:  # ECMA array
        return _amf0_props(buf, i + 4)
    if marker == 0x0A:  # strict array
        count = struct.unpack_from(">I", buf, i)[0]
        i += 4
        items = []
        for _ in range(count):
            item, i = _amf0_value(buf, i)
            items.append(item)
        return items, i
    if marker == 0x0B:  # date
        return struct.unpack_from(">d", buf, i)[0], i + 10
    if marker == 0x0C:  # long string
        length = struct.unpack_from(">I", buf, i)[0]
        i += 4
        return buf[i : i + length].decode("utf-8", "replace"), i + length
    raise ValueError(f"unsupported AMF0 marker 0x{marker:02x}")


def _amf0_string(buf: bytes, i: int):
    length = struct.unpack_from(">H", buf, i)[0]
    i += 2
    return buf[i : i + length].decode("utf-8", "replace"), i + length


def _amf0_props(buf: bytes, i: int):
    out: dict = {}
    while i < len(buf):
        key, i = _amf0_string(buf, i)
        if not key and i < len(buf) and buf[i] == 0x09:
            return out, i + 1
        value, i = _amf0_value(buf, i)
        out[key] = value
    return out, i

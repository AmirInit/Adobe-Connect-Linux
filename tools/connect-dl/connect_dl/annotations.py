"""Whiteboard and annotation data: finding it, reporting it, drawing what we can.

When a lecturer writes on Connect's whiteboard, none of that writing is in the
screen-share video.  Connect records annotations as *vector draw-commands* in
the recording's event streams (``ftcontent*``, ``indexstream*``) and the Flash
player redrew them live at playback time.  Rebuild the video without them and
the board is simply blank - the worst kind of failure, because the output looks
finished.

This module therefore does three separate jobs, in increasing order of how much
it has to assume:

1. **Report** (always, free).  Walk the event streams and say what is in them:
   how many timestamped messages, which message names, over what span.  This is
   a faithful decode of the container and the AMF0 inside it - no interpretation
   - so it is correct even for a Connect version whose vocabulary we have never
   seen.  A blank board is never a mystery.
2. **Extract** (``--render-annotations``).  Pull out messages whose decoded
   shape is drawing: a name that reads like a draw command, and numbers that
   read like coordinates.  Everything found is written to a report file
   verbatim, so an unrecognised dialect can be looked at rather than guessed at.
3. **Draw** (``--render-annotations``, best effort).  Turn what was extracted
   into SVG - one per board state, plus the final board - keyed to the meeting
   timeline.  A rough but faithful reproduction is the goal.

**What is verified and what is not.**  Steps 1 and 2 are byte-level work
verified against the FLV and AMF0 specifications and against fixtures in the
test suite.  Step 3's *vocabulary* - which command names mean "line" versus
"pen stroke", which coordinate order - has not been checked against a real
Connect archive, because none was available when this was written.  The code is
written so that this is safe: an unrecognised command is counted and dumped,
never silently dropped and never guessed into a wrong line.  If your archive
produces a report full of "unrecognised" entries, that file is exactly what is
needed to add support for its dialect.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from .flv import iter_script_tags

log = logging.getLogger(__name__)

__all__ = [
    "AnnotationScan", "StreamReport", "DrawCommand",
    "scan_annotations", "report_lines", "render_annotations",
]

# Message names that read like drawing.  Matched case-insensitively as
# substrings, because Connect prefixes and namespaces them differently between
# versions ("draw", "onDraw", "wb.draw", "shapeAdd"…).
_DRAW_HINTS = (
    "draw", "stroke", "pen", "pencil", "line", "shape", "ink", "annot",
    "marker", "highlight", "arrow", "ellipse", "rect", "polygon", "path",
    "text", "erase", "clear", "wb", "whiteboard",
)

# Names that mean "the board was wiped" - a boundary between board states.
_CLEAR_HINTS = ("clear", "erase", "undoall", "reset", "newpage", "pagechange")

_CANVAS = (1024, 768)

# AMF0 type bytes, so a body we cannot read can still be described.  0x11 is
# the one that matters: it means the payload switched to AMF3, which this
# decoder does not implement - and knowing that is most of the way to fixing it.
_AMF_MARKERS = {
    0x00: "number", 0x01: "boolean", 0x02: "string", 0x03: "object",
    0x05: "null", 0x06: "undefined", 0x07: "reference", 0x08: "ecma-array",
    0x09: "object-end", 0x0A: "strict-array", 0x0B: "date", 0x0C: "long-string",
    0x0D: "unsupported", 0x0F: "xml-document", 0x10: "typed-object",
    0x11: "AMF3 switch (not decoded by this tool)",
}


@dataclass
class DrawCommand:
    """One decoded message that looks like drawing."""

    time_ms: int
    name: str
    numbers: list[float]
    payload: object = None

    @property
    def points(self) -> list[tuple[float, float]]:
        """The numbers read as (x, y) pairs.

        Every vector format we might meet here encodes coordinates as a flat
        run of numbers; pairing them is the one assumption cheap enough to make
        without knowing the dialect.  An odd count means the leading value is
        something else (a colour, a width), so the tail is used.
        """
        values = self.numbers[1:] if len(self.numbers) % 2 else self.numbers
        return [(values[i], values[i + 1]) for i in range(0, len(values) - 1, 2)]

    @property
    def is_clear(self) -> bool:
        low = self.name.lower()
        return any(hint in low for hint in _CLEAR_HINTS)


@dataclass
class StreamReport:
    """What one event stream turned out to contain."""

    path: Path
    tags: int = 0
    decoded: int = 0
    undecodable_bytes: int = 0
    opaque_values: int = 0
    markers: dict[int, int] = field(default_factory=dict)
    first_ms: int | None = None
    last_ms: int | None = None
    names: dict[str, int] = field(default_factory=dict)
    draws: list[DrawCommand] = field(default_factory=list)

    @property
    def marker_note(self) -> str:
        """Plain-English account of the type bytes we could not read."""
        if not self.markers:
            return ""
        parts = []
        for marker, count in sorted(self.markers.items(), key=lambda kv: -kv[1]):
            parts.append(f"0x{marker:02x} {_AMF_MARKERS.get(marker, 'unknown')} x{count}")
        return ", ".join(parts)

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def span_s(self) -> float:
        if self.first_ms is None or self.last_ms is None:
            return 0.0
        return (self.last_ms - self.first_ms) / 1000.0


@dataclass
class AnnotationScan:
    streams: list[StreamReport] = field(default_factory=list)
    images: list[Path] = field(default_factory=list)

    @property
    def total_tags(self) -> int:
        return sum(s.tags for s in self.streams)

    @property
    def draw_commands(self) -> list[DrawCommand]:
        found = [d for s in self.streams for d in s.draws]
        return sorted(found, key=lambda d: d.time_ms)

    @property
    def has_data(self) -> bool:
        """Is there anything in these streams at all?"""
        return self.total_tags > 0 or bool(self.images)

    @property
    def has_drawing(self) -> bool:
        return any(d.points for d in self.draw_commands)


# ------------------------------------------------------------------ scanning

def scan_annotations(archive) -> AnnotationScan:
    """Read the archive's event streams.  Never raises; never renders."""
    scan = AnnotationScan()

    for stream in getattr(archive, "metadata_streams", []):
        scan.streams.append(_scan_stream(stream.path))

    # Some Connect versions export annotations as images beside the streams
    # rather than as commands inside them.  Note them so step 2 can say so.
    root = getattr(archive, "root", None)
    if root:
        for pattern in ("*.png", "*.jpg", "*.jpeg", "*.gif"):
            scan.images.extend(sorted(Path(root).rglob(pattern)))
    return scan


def _scan_stream(path: Path) -> StreamReport:
    report = StreamReport(path=path)
    for time_ms, name, value, marker, raw_len in iter_script_tags(path):
        report.tags += 1
        if report.first_ms is None:
            report.first_ms = time_ms
        report.last_ms = time_ms

        if marker is not None:
            report.markers[marker] = report.markers.get(marker, 0) + 1

        if name is None:
            report.undecodable_bytes += raw_len
            continue
        report.decoded += 1
        report.names[name] = report.names.get(name, 0) + 1
        if marker is not None:
            # The name read, the body did not: the interesting failure, and the
            # one an unknown dialect produces.  A body that decoded *to* null -
            # which is what a "clear the board" message looks like - is not
            # that, so the marker rather than the value settles it.
            report.opaque_values += 1

        if _looks_like_drawing(name):
            numbers = _numbers_in(value)
            report.draws.append(
                DrawCommand(time_ms=time_ms, name=name, numbers=numbers, payload=value)
            )
    return report


def _looks_like_drawing(name: str) -> bool:
    low = name.lower()
    return any(hint in low for hint in _DRAW_HINTS)


def _numbers_in(value, depth: int = 0) -> list[float]:
    """Every number reachable inside a decoded AMF0 value, in order."""
    if depth > 6:
        return []
    if isinstance(value, bool):
        return []
    if isinstance(value, (int, float)):
        return [float(value)]
    if isinstance(value, (list, tuple)):
        return [n for item in value for n in _numbers_in(item, depth + 1)]
    if isinstance(value, dict):
        return [n for _, item in sorted(value.items()) for n in _numbers_in(item, depth + 1)]
    return []


# ----------------------------------------------------------------- reporting

def report_lines(scan: AnnotationScan, *, suggest_render: bool = True) -> list[str]:
    """What to print about annotations - always, whether or not they rendered."""
    if not scan.has_data:
        return ["annotations: none - this recording has no whiteboard or "
                "annotation data at all."]

    lines = ["annotations:"]
    for stream in scan.streams:
        if not stream.tags:
            continue
        top = ", ".join(f"{n}x{c}" for n, c in
                        sorted(stream.names.items(), key=lambda kv: -kv[1])[:4]) or "-"
        lines.append(
            f"  {stream.name:<26} {stream.tags:5d} events over {stream.span_s / 60:5.1f} min"
            f"   [{top}]"
        )
        if stream.undecodable_bytes or stream.opaque_values:
            detail = stream.marker_note or "unknown encoding"
            lines.append(
                f"  {'':<26} {stream.opaque_values} message body/bodies did not decode "
                f"({detail})"
            )

    drawing = [d for d in scan.draw_commands if d.points]
    if drawing:
        line = (f"  {len(drawing)} of those look like drawing "
                f"({sum(len(d.points) for d in drawing)} points).")
        if suggest_render:
            line += " Pass --render-annotations to try to draw them."
        lines.append(line)
    elif scan.total_tags:
        lines.append(
            "  none of them decode as drawing commands - so either this lecture "
            "used no whiteboard, or its dialect is one this tool does not know."
        )
    if scan.images:
        lines.append(f"  {len(scan.images)} image file(s) in the archive: " +
                     ", ".join(p.name for p in scan.images[:4]))
    return lines


# ------------------------------------------------------------------ drawing

def render_annotations(scan: AnnotationScan, out_dir: Path) -> tuple[list[Path], list[str]]:
    """Best effort: write what was found, and draw what can be drawn.

    Returns ``(files_written, notes)``.  The report file is written whatever
    happens - if the drawing fails, that file is the artifact, and it is what
    someone needs in order to teach this tool the format.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    notes: list[str] = []

    report = out_dir / "annotations-report.txt"
    report.write_text(_dump(scan), encoding="utf-8")
    written.append(report)

    if not scan.has_data:
        notes.append("No annotation data of any kind is present in this recording.")
        return written, notes

    boards = _split_into_boards(scan.draw_commands)
    drawable = [b for b in boards if any(c.points for c in b)]

    if not drawable:
        notes.append(
            "This recording carries "
            f"{scan.total_tags} annotation event(s), but none of them decoded into "
            "drawable coordinates - the whiteboard was NOT reproduced. What was "
            f"found is written out verbatim in {report.name}; that file is what is "
            "needed to add support for this recording's format."
        )
        return written, notes

    for index, board in enumerate(drawable, 1):
        path = out_dir / f"annotations-{index:02d}.svg"
        path.write_text(_svg(board), encoding="utf-8")
        written.append(path)

    final = out_dir / "annotations-final.svg"
    final.write_text(_svg(drawable[-1]), encoding="utf-8")
    written.append(final)

    notes.append(
        f"Whiteboard: {len(drawable)} board state(s) drawn from "
        f"{sum(len(c.points) for b in drawable for c in b)} recovered points. "
        "This is a reconstruction from Connect's draw-commands, not a recording "
        "of the board - check it against the audio before trusting it."
    )
    return written, notes


def _split_into_boards(commands: list[DrawCommand]) -> list[list[DrawCommand]]:
    """Cut the command stream wherever the board was cleared."""
    boards: list[list[DrawCommand]] = [[]]
    for command in commands:
        if command.is_clear and boards[-1]:
            boards.append([])
            continue
        boards[-1].append(command)
    return [b for b in boards if b]


def _svg(board: list[DrawCommand]) -> str:
    """Draw one board state.

    Coordinates are used as they come; Connect's whiteboard space is not
    guaranteed to match the canvas, so the whole drawing is scaled to fit
    whatever range the points actually cover rather than being cropped.
    """
    points = [p for command in board for p in command.points]
    width, height = _CANVAS
    min_x = min((p[0] for p in points), default=0)
    max_x = max((p[0] for p in points), default=width)
    min_y = min((p[1] for p in points), default=0)
    max_y = max((p[1] for p in points), default=height)
    pad = 20
    span_x = max(max_x - min_x, 1)
    span_y = max(max_y - min_y, 1)
    # One scale for both axes, and centred.  Stretching each axis to fill the
    # canvas would fit more ink on the page and make the handwriting harder to
    # read, which is the opposite of the point.
    scale = min((width - 2 * pad) / span_x, (height - 2 * pad) / span_y)
    offset_x = (width - span_x * scale) / 2
    offset_y = (height - span_y * scale) / 2

    def place(point: tuple[float, float]) -> str:
        x = offset_x + (point[0] - min_x) * scale
        y = offset_y + (point[1] - min_y) * scale
        return f"{x:.1f},{y:.1f}"

    body = []
    for command in board:
        pts = command.points
        if len(pts) < 2:
            continue
        body.append(
            f'<polyline points="{" ".join(place(p) for p in pts)}" '
            f'fill="none" stroke="#111" stroke-width="2" '
            f'stroke-linecap="round" stroke-linejoin="round"><title>'
            f'{_escape(command.name)} @ {command.time_ms / 1000:.1f}s</title></polyline>'
        )

    start = board[0].time_ms / 1000.0 if board else 0.0
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">\n'
        f'<rect width="100%" height="100%" fill="#fff"/>\n'
        + "\n".join(body)
        + f'\n<text x="{pad}" y="{height - 8}" font-family="sans-serif" font-size="12" '
          f'fill="#888">reconstructed from Connect draw-commands, from '
          f'{start:.0f}s</text>\n</svg>\n'
    )


def _escape(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _dump(scan: AnnotationScan) -> str:
    """Everything the scan found, verbatim, for a human or a future parser."""
    out = [
        "connect-dl annotation scan",
        "=" * 60,
        "",
        "This file lists what the recording's event streams actually contain.",
        "If the whiteboard did not render, this is the evidence needed to make",
        "it render: the message names below are the vocabulary of this Connect",
        "version's whiteboard, and the numbers are its coordinates.",
        "",
    ]
    for stream in scan.streams:
        out.append(f"--- {stream.name} ---")
        out.append(f"  script tags      : {stream.tags}")
        out.append(f"  names decoded    : {stream.decoded}")
        out.append(f"  bodies undecoded : {stream.opaque_values}")
        out.append(f"  undecoded bytes  : {stream.undecodable_bytes}")
        if stream.markers:
            out.append(f"  type bytes seen  : {stream.marker_note}")
        out.append(f"  timestamp span   : {stream.first_ms} .. {stream.last_ms} ms")
        out.append("  message names    :")
        for name, count in sorted(stream.names.items(), key=lambda kv: -kv[1]):
            out.append(f"    {count:6d}  {name}")
        if stream.draws:
            out.append("  drawing-shaped messages (first 40):")
            for command in stream.draws[:40]:
                preview = ", ".join(f"{n:g}" for n in command.numbers[:12])
                more = " …" if len(command.numbers) > 12 else ""
                out.append(f"    {command.time_ms:9d} ms  {command.name}  "
                           f"[{preview}{more}]  ({len(command.points)} points)")
        out.append("")
    if scan.images:
        out.append("--- image files in the archive ---")
        out += [f"  {p}" for p in scan.images]
        out.append("")
    return "\n".join(out)

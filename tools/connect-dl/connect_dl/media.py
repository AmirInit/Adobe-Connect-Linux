"""Turning the loose FLV streams into files a normal player can open.

Two products come out of here:

* **the lecture audio** - every microphone segment placed at its true position
  on the meeting timeline and mixed down to one continuous track.  This is the
  deliverable that matters most: it survives even when the video streams are
  unreadable, and it is what gets played back at 2x.
* **the lecture video** - the shared screen laid onto a fixed canvas at the
  right moments, with the webcam as picture-in-picture and the mixed audio
  attached.

Both are built by generating an ffmpeg filtergraph rather than by shelling out
repeatedly, so a two-hour recording is one pass rather than dozens.

Nothing here assumes a modern ffmpeg beyond the filters named; where a filter
option is recent (``amix``'s ``normalize``) the call is retried without it.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .archive import RecordingArchive, Stream

log = logging.getLogger(__name__)

__all__ = [
    "FFmpegMissing", "ensure_ffmpeg", "render_audio", "render_video",
    "make_speed_variant", "atempo_chain", "transcode_for_web", "audio_branch",
]

AUDIO_ENCODERS = {
    "mp3": ["-c:a", "libmp3lame", "-q:a", "4"],
    "m4a": ["-c:a", "aac", "-b:a", "96k"],
    "opus": ["-c:a", "libopus", "-b:a", "48k"],
    "wav": ["-c:a", "pcm_s16le"],
}


class FFmpegMissing(RuntimeError):
    pass


@dataclass
class Tool:
    ffmpeg: str
    ffprobe: str | None


def ensure_ffmpeg(*, dry_run: bool = False) -> Tool:
    """Locate ffmpeg.  Under ``dry_run`` we only need a name to print."""
    if dry_run:
        return Tool(ffmpeg="ffmpeg", ffprobe=None)
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise FFmpegMissing(
            "ffmpeg was not found on PATH.\n"
            "  Debian/Ubuntu : sudo apt install ffmpeg\n"
            "  Fedora        : sudo dnf install ffmpeg\n"
            "  Arch          : sudo pacman -S ffmpeg\n"
            "ffmpeg is what decodes Connect's Nellymoser/Speex audio and Flash "
            "Screen Video, so it is required for anything beyond downloading."
        )
    return Tool(ffmpeg=ffmpeg, ffprobe=shutil.which("ffprobe"))


def run(cmd: list[str], *, dry_run: bool = False, retry_without: str | None = None) -> None:
    """Run ffmpeg, optionally retrying with one filter option stripped."""
    printable = " ".join(_quote(c) for c in cmd)
    if dry_run:
        print(printable)
        return
    log.debug("running: %s", printable)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0:
        return

    if retry_without and retry_without in printable:
        log.warning("ffmpeg rejected %r, retrying without it", retry_without)
        stripped = [c.replace(retry_without, "") for c in cmd]
        result = subprocess.run(stripped, capture_output=True, text=True)
        if result.returncode == 0:
            return

    tail = "\n".join(result.stderr.strip().splitlines()[-25:])
    raise RuntimeError(f"ffmpeg failed (exit {result.returncode})\ncommand: {printable}\n{tail}")


def _quote(token: str) -> str:
    return f"'{token}'" if any(ch in token for ch in " ;[]|'\"") else token


# ------------------------------------------------------------------- audio

def audio_branch(stream: Stream, input_index: int, label: str) -> str:
    """One filter branch: normalise a segment, level it, delay it into place.

    The order matters.  Levelling comes before the delay so the padding stays
    true silence rather than amplified nothing.
    """
    chain = [
        # Connect's VoIP audio is mono at odd sample rates; normalise first
        # so the mixer is not resampling several different formats at once.
        "aresample=async=1:first_pts=0",
        "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=mono",
    ]
    if stream.gain_db >= 0.1:
        chain.append(f"volume={stream.gain_db:.1f}dB")
    if stream.offset_ms > 0:
        chain.append(f"adelay={stream.offset_ms}:all=1")
    return f"[{input_index}:a]{','.join(chain)}[{label}]"


def build_audio_filter(streams: list[Stream]) -> tuple[str, str]:
    """Filtergraph placing each audio stream at its offset and mixing them.

    Returns ``(filtergraph, output_label)``.
    """
    if not streams:
        raise ValueError("no audio streams to mix")

    parts: list[str] = []
    labels: list[str] = []
    for index, stream in enumerate(streams):
        label = f"a{index}"
        parts.append(audio_branch(stream, index, label))
        labels.append(f"[{label}]")

    if len(labels) == 1:
        # A single source still needs a named output for -map to reference.
        parts.append(f"{labels[0]}anull[aout]")
    else:
        parts.append(
            f"{''.join(labels)}amix=inputs={len(labels)}"
            f":duration=longest:dropout_transition=0:normalize=0[aout]"
        )
    return ";".join(parts), "[aout]"


def render_audio(
    archive: RecordingArchive,
    out_path: Path,
    *,
    fmt: str = "mp3",
    streams: list[Stream] | None = None,
    tool: Tool | None = None,
    dry_run: bool = False,
) -> Path:
    """Mix every voice stream into one timeline-correct lecture track.

    ``streams`` narrows the mix (``--only-speaker``); by default every
    audio-bearing stream in the archive goes in.
    """
    tool = tool or ensure_ffmpeg(dry_run=dry_run)
    streams = archive.audio_streams if streams is None else streams
    if not streams:
        raise RuntimeError(
            "this recording contains no audio-bearing stream. "
            "If the class used a telephone bridge, the audio may not be in the "
            "archive at all and has to be requested from the Connect administrator."
        )

    graph, out_label = build_audio_filter(streams)
    cmd = [tool.ffmpeg, "-y", "-nostdin"]
    for stream in streams:
        cmd += ["-i", str(stream.path)]
    cmd += ["-filter_complex", graph, "-map", out_label, "-vn"]
    cmd += AUDIO_ENCODERS.get(fmt, AUDIO_ENCODERS["mp3"])
    if archive.title:
        cmd += ["-metadata", f"title={archive.title}"]
    cmd += [str(out_path)]

    log.info("mixing %d audio segment(s) into %s", len(streams), out_path.name)
    run(cmd, dry_run=dry_run, retry_without=":normalize=0")
    return out_path


# ------------------------------------------------------------------- video

def build_video_filter(
    screens: list[Stream],
    cameras: list[Stream],
    audio: list[Stream],
    *,
    width: int,
    height: int,
    fps: int,
    duration_s: float,
    pip_width: int = 240,
) -> tuple[str, list[str], str, str | None]:
    """Compose the shared screen (and webcam PiP) onto a fixed canvas.

    Returns ``(filtergraph, input_paths, video_label, audio_label)``.  Input 0
    is reserved for a black ``color`` source supplied by the caller: it defines
    the canvas and the full duration, so stretches where nothing was shared stay
    black instead of freezing on a stale frame.  ``input_paths`` are the real
    files, starting at slot 1.

    A file used for both its picture and its sound (``cameraVoip`` carries both)
    is opened once and referenced twice, so ffmpeg decodes it a single time.
    """
    inputs: list[str] = []
    slots: dict[str, int] = {}
    parts: list[str] = []

    def slot_for(stream: Stream) -> int:
        """Input index for a file, reusing one already opened."""
        key = str(stream.path)
        if key not in slots:
            inputs.append(key)
            slots[key] = len(inputs)  # slot 0 is the colour source
        return slots[key]

    current = "[0:v]"

    def overlay(stream: Stream, target_w: int, position: str) -> None:
        nonlocal current
        index = slot_for(stream)
        label = f"v{index}"
        chain = [
            f"scale={target_w}:-2:force_original_aspect_ratio=decrease",
            f"fps={fps}",
            "setpts=PTS-STARTPTS",
        ]
        if stream.offset_ms > 0:
            # tpad prepends blank frames so the segment lands at its real time.
            chain.append(
                f"tpad=start_duration={stream.offset_ms / 1000:.3f}:start_mode=add:color=black"
            )
        parts.append(f"[{index}:v]{','.join(chain)}[{label}]")
        nxt = f"[c{index}]"
        parts.append(
            f"{current}[{label}]overlay={position}:eof_action=pass:repeatlast=0{nxt}"
        )
        current = nxt

    for stream in screens:
        overlay(stream, width, "(W-w)/2:(H-h)/2")
    for stream in cameras:
        overlay(stream, pip_width, "W-w-16:H-h-16")

    parts.append(f"{current}format=yuv420p[vout]")

    audio_label = None
    if audio:
        labels = []
        for stream in audio:
            index = slot_for(stream)
            label = f"pa{index}"
            parts.append(audio_branch(stream, index, label))
            labels.append(f"[{label}]")
        if len(labels) == 1:
            parts.append(f"{labels[0]}anull[aout]")
        else:
            parts.append(
                f"{''.join(labels)}amix=inputs={len(labels)}"
                f":duration=longest:dropout_transition=0:normalize=0[aout]"
            )
        audio_label = "[aout]"

    return ";".join(parts), inputs, "[vout]", audio_label


def render_video(
    archive: RecordingArchive,
    out_path: Path,
    *,
    width: int = 1280,
    height: int = 720,
    fps: int = 5,
    crf: int = 28,
    preset: str = "veryfast",
    camera_pip: bool = True,
    audio: list[Stream] | None = None,
    tool: Tool | None = None,
    dry_run: bool = False,
) -> Path:
    """Render the reconstructed lecture video."""
    tool = tool or ensure_ffmpeg(dry_run=dry_run)
    screens = archive.video_streams
    if not screens:
        raise RuntimeError("no screen-share or main video stream found in this recording")

    cameras = archive.camera_streams if camera_pip else []
    # When the camera is the only picture there is, promote it instead of
    # tucking it into a corner of an otherwise black frame.
    if cameras and screens and screens[0].role.value == "camera":
        cameras = []

    audio = archive.audio_streams if audio is None else audio
    duration_s = max(archive.duration_ms / 1000.0, 1.0)

    graph, inputs, vlabel, alabel = build_video_filter(
        screens, cameras, audio,
        width=width, height=height, fps=fps, duration_s=duration_s,
    )

    cmd = [tool.ffmpeg, "-y", "-nostdin", "-f", "lavfi", "-i",
           f"color=c=black:s={width}x{height}:r={fps}:d={duration_s:.3f}"]
    for path in inputs:
        cmd += ["-i", path]
    cmd += ["-filter_complex", graph, "-map", vlabel]
    if alabel:
        cmd += ["-map", alabel, "-c:a", "aac", "-b:a", "96k"]
    cmd += [
        "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        "-t", f"{duration_s:.3f}",
    ]
    if archive.title:
        cmd += ["-metadata", f"title={archive.title}"]
    cmd += [str(out_path)]

    log.info("rendering video: %d screen + %d camera + %d audio stream(s)",
             len(screens), len(cameras), len(audio))
    run(cmd, dry_run=dry_run, retry_without=":normalize=0")
    return out_path


# -------------------------------------------------------------- speed / web

def atempo_chain(speed: float) -> str:
    """Express an arbitrary speed as a chain of in-range ``atempo`` filters.

    ``atempo`` accepts 0.5-2.0 on older ffmpeg builds, so 3x becomes
    ``atempo=1.5,atempo=2.0``.
    """
    if speed <= 0:
        raise ValueError("speed must be positive")
    if abs(speed - 1.0) < 1e-6:
        return "anull"

    factors: list[float] = []
    remaining = speed
    while remaining > 2.0:
        factors.append(2.0)
        remaining /= 2.0
    while remaining < 0.5:
        factors.append(0.5)
        remaining /= 0.5
    factors.append(remaining)
    return ",".join(f"atempo={f:g}" for f in factors)


def make_speed_variant(
    src: Path, out_path: Path, speed: float, *,
    tool: Tool | None = None, dry_run: bool = False,
) -> Path:
    """Bake a permanently sped-up copy, for players with no speed control."""
    tool = tool or ensure_ffmpeg(dry_run=dry_run)
    cmd = [
        tool.ffmpeg, "-y", "-nostdin", "-i", str(src),
        "-filter:a", atempo_chain(speed), "-vn",
    ]
    cmd += AUDIO_ENCODERS.get(out_path.suffix.lstrip("."), AUDIO_ENCODERS["mp3"])
    cmd += [str(out_path)]
    log.info("writing %gx copy to %s", speed, out_path.name)
    run(cmd, dry_run=dry_run)
    return out_path


def transcode_for_web(
    stream: Stream, out_path: Path, *,
    width: int = 1280, fps: int = 5, crf: int = 30,
    tool: Tool | None = None, dry_run: bool = False,
) -> Path:
    """Re-encode one FLV into browser-playable MP4 for the offline player."""
    tool = tool or ensure_ffmpeg(dry_run=dry_run)
    cmd = [
        tool.ffmpeg, "-y", "-nostdin", "-i", str(stream.path),
        "-vf", f"scale={width}:-2:force_original_aspect_ratio=decrease,fps={fps}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
    ]
    cmd += ["-c:a", "aac", "-b:a", "96k"] if stream.has_audio else ["-an"]
    cmd += [str(out_path)]
    run(cmd, dry_run=dry_run)
    return out_path

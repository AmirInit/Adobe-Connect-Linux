"""Bringing every microphone in the room to the same loudness.

Every microphone in a Connect room has its own gain, and nothing normalises
them.  On a real lecture the professor's headset and a student answering from a
laptop across the room measured 29 dB apart: the student is *present* in the
recording and completely inaudible in it.  That is not something the volume
knob fixes, because turning it up turns the professor into a shout.

So each segment is measured on its own and given its own gain, chosen from the
*mean* level rather than the peak - one stray click or chair scrape should not
hold a whole quiet segment down - and then capped three ways:

* by the peak ceiling, so boosting a segment cannot push it into clipping,
* by an absolute maximum, so a segment that is nothing but room tone is not
  amplified into a wall of hiss,
* at zero, so this only ever raises a quiet mic and never ducks a good one.

Measuring costs one decode pass per segment (ffmpeg's ``volumedetect``), which
is why ``--no-level`` exists.
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

__all__ = [
    "measure_level", "plan_gain", "level_streams", "quiet_report",
    "TARGET_MEAN_DB", "PEAK_CEILING_DB", "MAX_BOOST_DB", "QUIET_THRESHOLD_DB",
]

TARGET_MEAN_DB = -20.0    # where a well-levelled voice sits
PEAK_CEILING_DB = -1.5    # never boost a segment past this, to avoid clipping
MAX_BOOST_DB = 30.0       # a limit, so near-silence is not amplified into hiss
QUIET_THRESHOLD_DB = 8.0  # a segment raised by more than this is worth saying
SILENCE_FLOOR_DB = -70.0  # below this there is no voice to rescue

_MEAN_RE = re.compile(r"mean_volume:\s*(-?[\d.]+) dB")
_PEAK_RE = re.compile(r"max_volume:\s*(-?[\d.]+) dB")


def measure_level(path: Path, *, ffmpeg: str = "ffmpeg", timeout: int = 900) -> tuple[float, float] | None:
    """Return ``(mean_dB, peak_dB)`` for a file's audio, or ``None``.

    ``None`` means "could not measure" - a stream with no audio, an unreadable
    file, or no ffmpeg.  Callers treat that as "leave this one alone".
    """
    cmd = [
        ffmpeg, "-hide_banner", "-nostdin", "-i", str(path),
        "-af", "volumedetect", "-f", "null", "-",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.debug("could not measure %s: %s", path.name, exc)
        return None

    mean = _MEAN_RE.search(result.stderr)
    peak = _PEAK_RE.search(result.stderr)
    if not mean or not peak:
        return None
    return float(mean.group(1)), float(peak.group(1))


def plan_gain(mean_db: float, max_db: float) -> float:
    """How much to raise a segment measuring ``mean_db`` / ``max_db``.

    Pure arithmetic, so the policy can be tested without decoding anything.
    """
    if mean_db <= SILENCE_FLOOR_DB:
        return 0.0                      # effectively silence; leave it alone
    wanted = TARGET_MEAN_DB - mean_db   # what it would take to hit the target
    headroom = PEAK_CEILING_DB - max_db  # what the peak allows
    return round(max(0.0, min(wanted, headroom, MAX_BOOST_DB)), 1)


def level_streams(streams, *, ffmpeg: str = "ffmpeg") -> list:
    """Measure and set ``gain_db`` on every audio-bearing stream.

    Returns the streams raised by more than :data:`QUIET_THRESHOLD_DB` - the
    ones worth telling the user about, because without the boost those voices
    are buried under the main microphone.
    """
    audible = [s for s in streams if s.has_audio]
    for stream in audible:
        measured = measure_level(stream.path, ffmpeg=ffmpeg)
        if measured is None:
            continue
        stream.mean_db, stream.max_db = measured
        stream.gain_db = plan_gain(stream.mean_db, stream.max_db)
        log.debug("%s: mean %.1f dB, peak %.1f dB -> +%.1f dB",
                  stream.name, stream.mean_db, stream.max_db, stream.gain_db)
    return [s for s in audible if s.gain_db >= QUIET_THRESHOLD_DB]


def quiet_report(raised: list) -> list[str]:
    """Human-readable lines describing which microphones were brought up."""
    if not raised:
        return []
    lines = ["quiet microphones raised so they can be heard:"]
    for stream in sorted(raised, key=lambda s: -s.gain_db):
        lines.append(f"  {stream.name:<28} {stream.mean_db:6.1f} dB  ->  +{stream.gain_db:.0f} dB")
    lines.append("  without this these voices sit under the main mic; use --no-level to keep")
    lines.append("  the original levels.")
    return lines

"""Recovering the meeting's event track from the recording's XML sidecars.

Connect stores the seek index, pod layout changes and the chat transcript as
XML alongside the media.  The schemas vary between Connect versions and are not
documented, so everything here is deliberately tolerant: we look for elements
that carry a time-like attribute together with something that reads as a label
or a message, and ignore whatever we do not recognise.

None of this is required for playback - it only enriches the offline player
with chapter markers and a chat transcript, and is skipped silently when the
recording has no usable XML.
"""

from __future__ import annotations

import html
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

__all__ = ["Marker", "ChatLine", "parse_events", "Events"]

_TIME_KEYS = ("time", "ts", "timestamp", "start", "starttime", "start-time", "offset", "at")
_NAME_KEYS = ("name", "label", "title", "text", "value", "description", "caption")
_SENDER_KEYS = ("from", "sender", "user", "username", "author", "displayname", "name")
_BODY_KEYS = ("message", "text", "body", "value", "content")


@dataclass
class Marker:
    time_ms: int
    label: str

    @property
    def time_s(self) -> float:
        return self.time_ms / 1000.0


@dataclass
class ChatLine:
    time_ms: int
    sender: str
    message: str

    @property
    def time_s(self) -> float:
        return self.time_ms / 1000.0


@dataclass
class Events:
    markers: list[Marker]
    chat: list[ChatLine]

    def __bool__(self) -> bool:
        return bool(self.markers or self.chat)


def parse_events(xml_files: list[Path], *, shift_ms: int = 0, duration_ms: int = 0) -> Events:
    """Extract chapter markers and chat lines from a recording's XML files.

    ``shift_ms`` is subtracted from every timestamp so events line up with the
    normalised media timeline; entries falling outside the recording are dropped.
    """
    markers: list[Marker] = []
    chat: list[ChatLine] = []

    for path in xml_files:
        try:
            tree = ET.parse(path)
        except (ET.ParseError, OSError) as exc:
            log.debug("skipping %s: %s", path.name, exc)
            continue

        is_chat = "chat" in path.name.lower()
        for element in tree.iter():
            time_ms = _time_of(element)
            if time_ms is None:
                continue
            if is_chat:
                line = _as_chat(element, time_ms)
                if line:
                    chat.append(line)
            else:
                marker = _as_marker(element, time_ms)
                if marker:
                    markers.append(marker)

    markers = _tidy(markers, shift_ms, duration_ms, key=lambda m: (m.time_ms, m.label))
    chat = _tidy(chat, shift_ms, duration_ms, key=lambda c: (c.time_ms, c.sender, c.message))

    if markers or chat:
        log.info("recovered %d chapter marker(s) and %d chat line(s)", len(markers), len(chat))
    return Events(markers=markers, chat=chat)


def _tidy(items, shift_ms, duration_ms, key):
    out = []
    seen = set()
    for item in items:
        item.time_ms -= shift_ms
        if item.time_ms < 0:
            continue
        if duration_ms and item.time_ms > duration_ms + 5000:
            continue
        signature = key(item)
        if signature in seen:
            continue
        seen.add(signature)
        out.append(item)
    out.sort(key=lambda i: i.time_ms)
    return out


def _time_of(element: ET.Element) -> int | None:
    for key, value in element.attrib.items():
        if key.lower().replace("_", "-") not in _TIME_KEYS:
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number >= 0:
            return int(number)
    return None


def _pick(element: ET.Element, keys: tuple[str, ...]) -> str | None:
    for key in keys:
        for attr, value in element.attrib.items():
            if attr.lower().replace("_", "-") == key and value and value.strip():
                return _clean(value)
    for key in keys:
        child = element.find(key)
        if child is not None and child.text and child.text.strip():
            return _clean(child.text)
    return None


def _clean(text: str) -> str:
    text = html.unescape(text)
    text = re.sub(r"<[^>]+>", "", text)          # Connect wraps chat in markup
    return re.sub(r"\s+", " ", text).strip()


def _as_marker(element: ET.Element, time_ms: int) -> Marker | None:
    label = _pick(element, _NAME_KEYS)
    if not label:
        text = (element.text or "").strip()
        label = _clean(text) if text else None
    if not label or len(label) > 160:
        return None
    if label.replace(".", "").isdigit():
        return None
    return Marker(time_ms=time_ms, label=label)


def _as_chat(element: ET.Element, time_ms: int) -> ChatLine | None:
    message = _pick(element, _BODY_KEYS)
    if not message:
        text = (element.text or "").strip()
        message = _clean(text) if text else None
    if not message:
        return None
    sender = _pick(element, _SENDER_KEYS) or ""
    if sender == message:
        sender = ""
    return ChatLine(time_ms=time_ms, sender=sender, message=message)

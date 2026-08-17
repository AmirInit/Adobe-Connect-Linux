"""Parsing and normalisation of the many shapes an Adobe Connect link can take.

A user typically has one of these in hand:

  https://vadavc41.ec.iau.ir/p8fj3k2la9x/                  recording short link
  https://vadavc41.ec.iau.ir/l993retztu2a/                 meeting room link
  https://acc.example.edu/p8fj3k2la9x/?pbMode=normal       recording, player mode
  connectpro://vadavc41.ec.iau.ir/l993retztu2a/?session=…  what the desktop client gets
  https://acc.example.edu/p8fj3k2la9x/output/f.zip?…       an already-built asset link

They all reduce to (origin, url_path, session_token).  ``url_path`` is the SCO
"url-path" Adobe Connect uses as the public handle for a room or a recording.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse, urlunparse

__all__ = ["ConnectLink", "parse_link", "InvalidLink"]


class InvalidLink(ValueError):
    """Raised when a string cannot be read as an Adobe Connect link."""


# Path segments that are part of Connect's own plumbing rather than a SCO handle.
_RESERVED_SEGMENTS = {
    "api",
    "admin",
    "system",
    "common",
    "content",
    "flash",
    "output",
    "swf",
    "app",
    "connect",
}

# A SCO url-path is a short alphanumeric token.  Recordings conventionally start
# with "p", rooms with a letter, but neither is guaranteed across Connect
# versions, so we match structurally rather than by prefix.
_URL_PATH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{3,63}$")


@dataclass(frozen=True)
class ConnectLink:
    """A normalised Adobe Connect location."""

    origin: str
    """Scheme + host + port, e.g. ``https://acc.example.edu``."""

    url_path: str | None
    """The SCO handle without slashes, e.g. ``p8fj3k2la9x``.  ``None`` when the
    link only identifies a server (the account root)."""

    session: str | None = None
    """A BREEZESESSION value lifted from the query string, when present."""

    sco_id: str | None = None
    """A numeric sco-id, when the link carried one instead of a url-path."""

    @property
    def base(self) -> str:
        """The canonical playback URL, with trailing slash."""
        if not self.url_path:
            return self.origin + "/"
        return f"{self.origin}/{self.url_path}/"

    @property
    def api(self) -> str:
        return f"{self.origin}/api/xml"

    def asset(self, path: str) -> str:
        """URL for a file inside this SCO's output directory."""
        return self.base + path.lstrip("/")

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return self.base


def _clean_host_scheme(raw: str) -> str:
    """Map Connect's private schemes onto https."""
    lowered = raw.strip()
    for scheme in ("connectpro://", "connect://", "meeting://"):
        if lowered.lower().startswith(scheme):
            rest = lowered[len(scheme) :]
            # connectpro://https://host/... is a real form the launcher produces.
            if rest.lower().startswith(("http://", "https://")):
                return rest
            return "https://" + rest
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", lowered):
        return "https://" + lowered
    return lowered


def parse_link(raw: str) -> ConnectLink:
    """Turn any user-supplied Connect link into a :class:`ConnectLink`.

    Raises :class:`InvalidLink` if no host can be determined.
    """
    if not raw or not raw.strip():
        raise InvalidLink("empty link")

    parsed = urlparse(_clean_host_scheme(raw))
    if not parsed.netloc:
        raise InvalidLink(f"could not find a host in {raw!r}")

    scheme = "https" if parsed.scheme in ("", "http", "connectpro") else parsed.scheme
    origin = urlunparse((scheme, parsed.netloc, "", "", "", "")).rstrip("/")

    query = parse_qs(parsed.query)
    session = _first(query, "session", "BREEZESESSION", "breezesession")
    sco_id = _first(query, "sco-id", "sco_id", "scoId")

    url_path = _extract_url_path(parsed.path)

    return ConnectLink(origin=origin, url_path=url_path, session=session, sco_id=sco_id)


def _first(query: dict[str, list[str]], *keys: str) -> str | None:
    for key in keys:
        values = query.get(key)
        if values and values[0].strip():
            return values[0].strip()
    return None


def _extract_url_path(path: str) -> str | None:
    """Pick the SCO handle out of a URL path.

    The handle is the first segment that looks like a token and is not one of
    Connect's reserved paths.  Anything after it (``output/…``, ``index.html``)
    is ignored.
    """
    for segment in path.split("/"):
        segment = segment.strip()
        if not segment:
            continue
        if segment.lower() in _RESERVED_SEGMENTS:
            # Reserved segments never precede the handle in practice; stop so we
            # do not mistake "xml" in /api/xml for a SCO.
            return None
        if "." in segment:
            # A filename, not a handle.
            continue
        if _URL_PATH_RE.match(segment):
            return segment
    return None

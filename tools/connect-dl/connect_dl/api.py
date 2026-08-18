"""A small client for the Adobe Connect XML API.

Only the handful of actions needed to find and download a recording are
implemented.  Everything is stdlib so the tool can be dropped onto a machine and
run without installing anything.

Two authentication styles are supported:

* **Username / password** — calls ``action=login``.  On servers with Enhanced
  Security the cookie handed out before login is discarded once the user is
  authenticated, so a follow-up ``action=common-info`` call is required to pick
  up the session that actually carries the login.  We always do that second
  call; it is harmless on servers without the feature.
* **An existing BREEZESESSION** — copied out of a logged-in browser.  This is
  the escape hatch for accounts behind SSO/Shibboleth, where the XML login
  action is not usable at all.
"""

from __future__ import annotations

import gzip
import http.cookiejar
import io
import logging
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from .urls import ConnectLink

log = logging.getLogger(__name__)

__all__ = [
    "ConnectClient", "ConnectError", "AuthError", "Recording",
    "looks_like_login_page",
]

# Connect answers an unauthenticated asset request with HTTP 200 and the
# Connect Central login page rather than a 401/403.  Observed on Connect
# 10.8.0.  Without this check the download poll loop reads that page as "the
# server is still building the zip" and retries for the full poll timeout.
_LOGIN_MARKERS = (
    b"adobe connect central login",
    b"/common/scripts/breezeui.js",
    b'name="login"',
    b"session-timeout",
)


def looks_like_login_page(raw: bytes) -> bool:
    """True if ``raw`` is the start of Connect's HTML login page."""
    head = raw[:4096].lower()
    if b"<html" not in head and not head.lstrip().startswith(b"<"):
        return False
    return any(marker in head for marker in _LOGIN_MARKERS)

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0 Safari/537.36 connect-dl"
)


class ConnectError(RuntimeError):
    """The server rejected a request or returned something unusable."""


class AuthError(ConnectError):
    """Authentication failed, or the session is not valid (any more)."""


@dataclass
class Recording:
    """One archived meeting."""

    sco_id: str
    name: str
    url_path: str
    date_created: str | None = None
    duration: str | None = None
    folder: str | None = None

    def link(self, origin: str) -> ConnectLink:
        return ConnectLink(origin=origin, url_path=self.url_path.strip("/"))

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        bits = [self.name]
        if self.date_created:
            bits.append(self.date_created[:10])
        if self.duration:
            bits.append(self.duration)
        return "  ".join(bits)


@dataclass
class ConnectClient:
    """Authenticated conversation with one Adobe Connect server."""

    origin: str
    verify_tls: bool = True
    timeout: int = 60
    retries: int = 4
    _jar: http.cookiejar.CookieJar = field(default_factory=http.cookiejar.CookieJar, init=False)
    _opener: urllib.request.OpenerDirector = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if self.verify_tls:
            context = ssl.create_default_context()
        else:
            # Deliberately permissive: many university Connect deployments sit
            # behind middleboxes with self-signed certificates.  Mirrors the
            # --ignore-certificate-errors the desktop client already hardcodes.
            context = ssl._create_unverified_context()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=context),
            urllib.request.HTTPCookieProcessor(self._jar),
        )
        self._opener.addheaders = []

    # ---------------------------------------------------------------- plumbing

    @property
    def api_url(self) -> str:
        return f"{self.origin}/api/xml"

    def _open(self, url: str, *, data: bytes | None = None, headers: dict[str, str] | None = None):
        request = urllib.request.Request(url, data=data)
        request.add_header("User-Agent", USER_AGENT)
        request.add_header("Accept-Encoding", "gzip")
        for key, value in (headers or {}).items():
            request.add_header(key, value)

        last_error: Exception | None = None
        for attempt in range(self.retries):
            try:
                return self._opener.open(request, timeout=self.timeout)
            except urllib.error.HTTPError as exc:
                # 4xx are answers, not transport failures - do not retry them.
                if exc.code < 500:
                    raise
                last_error = exc
            except (urllib.error.URLError, TimeoutError, ssl.SSLError, OSError) as exc:
                last_error = exc
            backoff = 2 ** attempt
            log.warning("request to %s failed (%s), retrying in %ss", url, last_error, backoff)
            time.sleep(backoff)
        raise ConnectError(f"could not reach {url}: {last_error}")

    def get_bytes(self, url: str, *, headers: dict[str, str] | None = None) -> tuple[bytes, str]:
        """Fetch a URL, returning ``(body, content_type)`` with gzip undone."""
        with self._open(url, headers=headers) as response:
            raw = response.read()
            if response.headers.get("Content-Encoding") == "gzip":
                raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
            return raw, response.headers.get("Content-Type", "")

    def call(self, action: str, **params: str | None) -> ET.Element:
        """Invoke an XML API action and return the parsed ``<results>`` root."""
        query = {"action": action}
        query.update({k.replace("_", "-"): v for k, v in params.items() if v is not None})
        url = f"{self.api_url}?{urllib.parse.urlencode(query)}"

        body, _ = self.get_bytes(url)
        try:
            root = ET.fromstring(body)
        except ET.ParseError as exc:
            raise ConnectError(
                f"{action}: server did not return XML "
                f"(got {body[:120]!r}) - is {self.origin} really an Adobe Connect server?"
            ) from exc

        status = root.find("status")
        code = status.get("code") if status is not None else None
        if code and code != "ok":
            detail = ""
            if status is not None and len(status):
                child = status[0]
                detail = " " + " ".join(f"{k}={v}" for k, v in child.attrib.items())
            if code in ("no-access", "no-login"):
                raise AuthError(f"{action}: access denied ({code}{detail})")
            raise ConnectError(f"{action} failed: {code}{detail}")
        return root

    # ------------------------------------------------------------------ auth

    @property
    def session(self) -> str | None:
        for cookie in self._jar:
            if cookie.name == "BREEZESESSION":
                return cookie.value
        return None

    def set_session(self, value: str) -> None:
        """Install a BREEZESESSION captured from a browser."""
        host = urllib.parse.urlparse(self.origin).hostname or ""
        cookie = http.cookiejar.Cookie(
            version=0, name="BREEZESESSION", value=value, port=None, port_specified=False,
            domain=host, domain_specified=True, domain_initial_dot=False,
            path="/", path_specified=True, secure=False, expires=None, discard=True,
            comment=None, comment_url=None, rest={},
        )
        self._jar.set_cookie(cookie)

    def login(self, username: str, password: str) -> None:
        """Authenticate, handling the Enhanced Security session swap."""
        # Seed a session cookie first; Connect expects one to already exist.
        self.call("common-info")
        self.call("login", login=username, password=password)
        # With Enhanced Security the pre-login cookie is invalidated and a new
        # one is issued here.  Without it, this is a no-op sanity check.
        info = self.call("common-info")
        if not self._logged_in(info):
            raise AuthError(
                "login appeared to succeed but the session is not authenticated. "
                "If this account uses SSO, log in with a browser and pass the "
                "BREEZESESSION cookie via --session instead."
            )
        log.info("logged in as %s", username)

    def check_session(self) -> str | None:
        """Return the logged-in user's login name, or ``None`` if anonymous."""
        info = self.call("common-info")
        user = info.find(".//user/login")
        return user.text if user is not None and user.text else None

    @staticmethod
    def _logged_in(common_info: ET.Element) -> bool:
        return common_info.find(".//user") is not None

    # -------------------------------------------------------------- discovery

    def sco_info(self, sco_id: str) -> ET.Element:
        root = self.call("sco-info", sco_id=sco_id)
        sco = root.find(".//sco")
        if sco is None:
            raise ConnectError(f"sco-info returned no <sco> for id {sco_id}")
        return sco

    def resolve_url_path(self, url_path: str) -> ET.Element:
        """Look up a SCO by its public handle (``p8fj3k2la9x``)."""
        root = self.call("sco-by-url", url_path=f"/{url_path.strip('/')}/")
        sco = root.find(".//sco")
        if sco is None:
            raise ConnectError(f"no SCO found for /{url_path}/")
        return sco

    def list_recordings(self, folder_sco_id: str) -> list[Recording]:
        """All archives beneath a folder or meeting room."""
        root = self.call(
            "sco-expanded-contents", sco_id=folder_sco_id, filter_icon="archive"
        )
        return [self._to_recording(node) for node in root.findall(".//sco")]

    def meeting_recordings(self, meeting_sco_id: str) -> list[Recording]:
        """Archives belonging to one meeting room."""
        root = self.call("sco-contents", sco_id=meeting_sco_id, filter_icon="archive")
        found = [self._to_recording(node) for node in root.findall(".//sco")]
        if found:
            return found
        # Some deployments file recordings under the room rather than in it.
        return self.list_recordings(meeting_sco_id)

    @staticmethod
    def _to_recording(node: ET.Element) -> Recording:
        def text(tag: str) -> str | None:
            found = node.find(tag)
            return found.text if found is not None and found.text else None

        return Recording(
            sco_id=node.get("sco-id", ""),
            name=text("name") or "(untitled)",
            url_path=(text("url-path") or node.get("url-path") or "").strip("/"),
            date_created=text("date-created") or text("date-begin"),
            duration=text("duration"),
            folder=node.get("folder-id"),
        )

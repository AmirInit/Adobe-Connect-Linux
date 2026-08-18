"""Downloading the recording payload itself.

Adobe Connect exposes an archive of a recording's source files at::

    <recording-url>/output/<anything>.zip?download=zip

The server builds that zip on demand.  The first request often returns an HTML
"please wait" page or a zero-length body while the job runs, so the download is
a poll loop rather than a single GET.

Newer (HTML-client) recordings may instead expose a finished MP4 through the
same output directory.  We take that when it is offered, since it needs no
reconstruction at all.
"""

from __future__ import annotations

import logging
import os
import shutil
import time
import urllib.error
from dataclasses import dataclass
from pathlib import Path

from .api import AuthError, ConnectClient, ConnectError, looks_like_login_page
from .urls import ConnectLink

log = logging.getLogger(__name__)

__all__ = ["download_recording", "DownloadResult"]

# Candidate asset paths, best first.  ``filename`` is arbitrary - the server
# keys off the ?download=zip parameter, not the name - but some proxies care
# that the extension matches, so we keep it honest.
_ZIP_CANDIDATES = (
    "output/recording.zip?download=zip",
    "output/filename.zip?download=zip",
)
_MP4_CANDIDATES = (
    "output/recording.mp4?download=mp4",
    "output/filename.mp4?download=mp4",
)

_ZIP_MAGIC = b"PK\x03\x04"
_CHUNK = 1 << 16


@dataclass
class DownloadResult:
    path: Path
    kind: str  # "zip" | "mp4"
    size: int

    @property
    def is_zip(self) -> bool:
        return self.kind == "zip"


def download_recording(
    client: ConnectClient,
    link: ConnectLink,
    dest_dir: Path,
    *,
    poll_timeout: int = 1800,
    poll_interval: int = 15,
    prefer: str = "auto",
) -> DownloadResult:
    """Fetch a recording's payload into ``dest_dir``.

    ``prefer`` is ``"auto"`` (mp4 if offered, else zip), ``"zip"`` or ``"mp4"``.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)

    attempts: list[tuple[str, str]] = []
    if prefer in ("auto", "mp4"):
        attempts += [("mp4", c) for c in _MP4_CANDIDATES]
    if prefer in ("auto", "zip"):
        attempts += [("zip", c) for c in _ZIP_CANDIDATES]

    deadline = time.monotonic() + poll_timeout
    last_reason = "no attempt made"

    while time.monotonic() < deadline:
        for kind, candidate in attempts:
            url = link.asset(candidate)
            target = dest_dir / f"recording.{kind}"
            try:
                ok, reason = _try_download(client, url, target, kind)
            except urllib.error.HTTPError as exc:
                # 404 simply means this server does not offer that form.
                last_reason = f"HTTP {exc.code} for {candidate}"
                log.debug("%s -> %s", url, last_reason)
                continue

            if ok:
                size = target.stat().st_size
                log.info("downloaded %s (%s)", target.name, _human(size))
                return DownloadResult(path=target, kind=kind, size=size)

            last_reason = reason
            log.debug("%s -> %s", url, reason)

        remaining = int(deadline - time.monotonic())
        if remaining <= 0:
            break
        log.info(
            "server is still preparing the recording (%s); waiting %ss, %ss left",
            last_reason, poll_interval, remaining,
        )
        time.sleep(min(poll_interval, max(remaining, 1)))

    raise ConnectError(
        f"could not obtain a downloadable recording from {link.base} ({last_reason}). "
        "If the recording plays in a browser but will not download, the account "
        "may lack permission to fetch source files, or the link may point at a "
        "meeting room rather than an archive."
    )


def _try_download(
    client: ConnectClient, url: str, target: Path, kind: str
) -> tuple[bool, str]:
    """Stream one candidate URL.  Returns ``(succeeded, reason_if_not)``."""
    with client._open(url) as response:  # noqa: SLF001 - same package
        content_type = (response.headers.get("Content-Type") or "").lower()
        declared = response.headers.get("Content-Length")
        total = int(declared) if declared and declared.isdigit() else None

        head = response.read(len(_ZIP_MAGIC))

        if "html" in content_type or head.startswith(b"<"):
            body = head + response.read(4096)
            # A login page is a permanent answer, not a holding page.  Raising
            # here stops the caller polling for the full timeout against a
            # server that will never hand this account the file.
            if looks_like_login_page(body):
                raise AuthError(
                    f"{url}\n"
                    "  the server answered with the Adobe Connect login page, so this "
                    "recording is not public and no valid session was supplied.\n"
                    "  Pass credentials with -u/--user, or copy the BREEZESESSION "
                    "cookie out of a logged-in browser and pass --session "
                    "(required for SSO/Shibboleth accounts)."
                )
            snippet = body.decode("utf-8", "replace").strip()[:120]
            if kind == "zip":
                return False, f"not a zip yet (content-type {content_type!r}: {snippet!r})"
            return False, f"not an mp4 (content-type {content_type!r})"

        if kind == "zip" and head != _ZIP_MAGIC:
            rest = response.read(400)
            snippet = (head + rest).decode("utf-8", "replace").strip()[:120]
            return False, f"not a zip yet (content-type {content_type!r}: {snippet!r})"

        tmp = target.with_suffix(target.suffix + ".part")
        written = len(head)
        with open(tmp, "wb") as handle:
            handle.write(head)
            last_report = time.monotonic()
            while True:
                chunk = response.read(_CHUNK)
                if not chunk:
                    break
                handle.write(chunk)
                written += len(chunk)
                now = time.monotonic()
                if now - last_report > 2:
                    _progress(written, total)
                    last_report = now
        _progress(written, total, final=True)

    if written == 0:
        tmp.unlink(missing_ok=True)
        return False, "server returned an empty body"

    shutil.move(str(tmp), str(target))
    return True, ""


def _progress(written: int, total: int | None, *, final: bool = False) -> None:
    if total:
        pct = 100.0 * written / total
        line = f"  {_human(written)} / {_human(total)}  ({pct:5.1f}%)"
    else:
        line = f"  {_human(written)} downloaded"
    end = "\n" if final else "\r"
    print(line.ljust(60), end=end, flush=True)


def _human(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(n) < 1024 or unit == "GiB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} GiB"


def free_space(path: Path) -> int:
    return shutil.disk_usage(path).free if path.exists() else os.statvfs(".").f_bavail

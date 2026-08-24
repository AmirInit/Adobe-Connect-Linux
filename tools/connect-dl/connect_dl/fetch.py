"""Downloading the recording payload itself.

Adobe Connect exposes an archive of a recording's source files at::

    <recording-url>/output/<name>.zip?download=zip

The server builds that zip on demand.  The first request often returns an HTML
"please wait" page or a zero-length body while the job runs, so the download is
a poll loop rather than a single GET.

Three things about a real Connect 10.8 server shape this module:

* **The name matters.** ``output/class.zip`` is what the servers we have tested
  against actually serve.  ``output/recording.zip`` and ``output/recording.mp4``
  answer with the login page, which is indistinguishable from a genuine auth
  failure - so the candidate that works is tried first and the rest are only
  reached if it does not exist.
* **HTTP 200 is not success.** An unauthenticated request returns 200 with an
  HTML login page rather than 401.  Only the *body* tells the difference, so
  every candidate is checked for the zip magic (``PK``) or a zip content type
  before a single byte is kept.  The check reads four bytes, never the body:
  probing an 80 MB archive must not cost 80 MB.
* **Big transfers drop.** Archives of tens of megabytes routinely die
  mid-transfer.  Each attempt resumes from the bytes already on disk with a
  Range request instead of starting over, and the finished file is verified as
  a structurally valid zip before it is trusted.
"""

from __future__ import annotations

import logging
import os
import shutil
import time
import urllib.error
import zipfile
from dataclasses import dataclass
from pathlib import Path

from .api import AuthError, ConnectClient, ConnectError, looks_like_login_page
from .urls import ConnectLink

log = logging.getLogger(__name__)

__all__ = ["download_recording", "DownloadResult", "zip_is_complete"]

# Candidate asset paths, best first.  ``class.zip`` is the name observed on
# real servers; the others are kept because other Connect versions have used
# them and trying an extra URL is cheap.
_ZIP_CANDIDATES = (
    "output/class.zip?download=zip",
    "output/recording.zip?download=zip",
    "output/filename.zip?download=zip",
)
_MP4_CANDIDATES = (
    "output/recording.mp4?download=mp4",
    "output/filename.mp4?download=mp4",
)

_ZIP_MAGIC = b"PK\x03\x04"
_CHUNK = 1 << 16
_RESUME_ATTEMPTS = 5
# Below this a "zip" is a stub or an error page that happened to start with PK.
_MIN_PLAUSIBLE_ZIP = 100_000


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

    ``prefer`` is ``"auto"`` (the source zip, falling back to a finished mp4),
    ``"zip"`` or ``"mp4"``.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)

    attempts: list[tuple[str, str]] = []
    if prefer in ("auto", "zip"):
        attempts += [("zip", c) for c in _ZIP_CANDIDATES]
    if prefer in ("auto", "mp4"):
        attempts += [("mp4", c) for c in _MP4_CANDIDATES]

    deadline = time.monotonic() + poll_timeout
    last_reason = "no attempt made"

    while True:
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
        "may lack permission to fetch source files, or the meeting may never have "
        "been recorded."
    )


def _try_download(
    client: ConnectClient, url: str, target: Path, kind: str
) -> tuple[bool, str]:
    """Fetch one candidate URL, resuming after dropped connections.

    Returns ``(succeeded, reason_if_not)``.  Raises :class:`AuthError` when the
    server answers with its login page, which is a verdict rather than a
    holding page and must not be polled.
    """
    part = Path(str(target) + ".part")
    reason = "no attempt made"

    for attempt in range(1, _RESUME_ATTEMPTS + 1):
        have = part.stat().st_size if part.exists() else 0
        headers = {"Range": f"bytes={have}-"} if have else None
        dropped = False

        try:
            with client._open(url, headers=headers) as response:  # noqa: SLF001 - same package
                content_type = (response.headers.get("Content-Type") or "").lower()
                status = getattr(response, "status", 200)

                if have and status != 206:
                    # The server ignored the Range and is sending the whole file
                    # again; appending would corrupt it, so start clean.
                    log.info("%s does not support resuming; restarting the download", url)
                    part.unlink(missing_ok=True)
                    have = 0

                if have == 0:
                    head = response.read(len(_ZIP_MAGIC))
                    verdict = _reject(head, content_type, kind, url, response)
                    if verdict:
                        return False, verdict
                    part.write_bytes(head)
                    written = len(head)
                else:
                    written = have
                    log.info("resuming %s from %s", target.name, _human(have))

                total = _declared_total(response, have)
                dropped = not _stream_body(response, part, written, total)
        except urllib.error.HTTPError as exc:
            if exc.code == 416 and have:
                # "Range not satisfiable": we already hold at least as many
                # bytes as the server has, yet the file did not verify - so
                # what is on disk is junk, not a resumable prefix.
                log.info("server rejected the resume range; discarding %s", part.name)
                part.unlink(missing_ok=True)
                continue
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            # ConnectError from _open means the transport failed permanently.
            dropped = True
            reason = f"connection failed: {exc}"
            log.info("download attempt %d/%d failed: %s", attempt, _RESUME_ATTEMPTS, exc)
        except AuthError:
            # A login page is terminal.  This clause must stay *above* the
            # ConnectError one it derives from, or the abort is swallowed into a
            # retry reason and the caller polls the login page for the whole
            # timeout - the exact bug this module exists to prevent.
            raise
        except ConnectError as exc:
            return False, str(exc)

        got = part.stat().st_size if part.exists() else 0
        if not dropped and _payload_is_complete(part, kind):
            shutil.move(str(part), str(target))
            return True, ""

        if got == 0:
            part.unlink(missing_ok=True)
            return False, "server returned an empty body"

        if not dropped:
            reason = f"{kind} arrived truncated or corrupt ({_human(got)})"
        if have and got <= have:
            # No progress at all this round: the partial file is probably junk.
            log.info("no progress on resume; discarding %s and starting over", part.name)
            part.unlink(missing_ok=True)
        if attempt < _RESUME_ATTEMPTS:
            print(f"  transfer interrupted at {_human(got)}; resuming "
                  f"(attempt {attempt + 1}/{_RESUME_ATTEMPTS})", flush=True)

    return False, (
        f"{reason}. The partial download is kept at {part} - run the same "
        "command again to resume it."
    )


def _reject(head: bytes, content_type: str, kind: str, url: str, response) -> str | None:
    """Reason to refuse this response, or ``None`` to accept and stream it."""
    if "html" in content_type or head.startswith(b"<"):
        body = head + response.read(4096)
        # A login page is a permanent answer, not a holding page.  Raising here
        # stops the caller polling for the full timeout against a server that
        # will never hand this account the file.
        if looks_like_login_page(body):
            raise AuthError(
                f"{url}\n"
                "  the server answered with the Adobe Connect login page, so this "
                "recording is not public and no valid session was supplied.\n"
                "  Copy the BREEZESESSION cookie out of a logged-in browser and pass "
                "--session (developer tools -> Application -> Cookies), or sign in "
                "with -u/--user."
            )
        snippet = body.decode("utf-8", "replace").strip()[:120]
        return f"not a {kind} yet (content-type {content_type!r}: {snippet!r})"

    if kind == "zip" and head != _ZIP_MAGIC and "zip" not in content_type:
        snippet = (head + response.read(400)).decode("utf-8", "replace").strip()[:120]
        return f"not a zip yet (content-type {content_type!r}: {snippet!r})"
    return None


def _declared_total(response, already: int) -> int | None:
    declared = response.headers.get("Content-Length")
    if not declared or not str(declared).strip().isdigit():
        return None
    return int(declared) + already


def _stream_body(response, part: Path, written: int, total: int | None) -> bool:
    """Append the response body to ``part``.  False if the connection died."""
    last_report = time.monotonic()
    try:
        with open(part, "ab") as handle:
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
    except (OSError, urllib.error.URLError, TimeoutError, ValueError) as exc:
        # The bytes received so far stay on disk for the next attempt.
        log.info("transfer interrupted after %s: %s", _human(written), exc)
        _progress(written, total, final=True)
        return False
    _progress(written, total, final=True)
    if total is not None and written < total:
        return False
    return True


def _payload_is_complete(path: Path, kind: str) -> bool:
    if not path.exists() or path.stat().st_size == 0:
        return False
    if kind != "zip":
        return True
    return zip_is_complete(path)


def zip_is_complete(path: Path) -> bool:
    """True if ``path`` is a structurally valid zip.

    A zip's central directory sits at the *end* of the file, so reading it is
    both cheap and exactly the check that catches a truncated download.  We do
    not CRC every member (``testzip``): that decompresses the whole archive a
    second time, and a Range-resumed transfer is byte-exact where it is not
    truncated.
    """
    try:
        if path.stat().st_size < _MIN_PLAUSIBLE_ZIP:
            return False
        with zipfile.ZipFile(path) as zf:
            return bool(zf.namelist())
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile):
        return False


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

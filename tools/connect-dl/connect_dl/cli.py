"""Command line entry point."""

from __future__ import annotations

import argparse
import getpass
import logging
import os
import re
import sys
from pathlib import Path

from . import __version__
from .api import AuthError, ConnectClient, ConnectError, Recording  # noqa: F401
from .archive import RecordingArchive, Role, unpack
from .fetch import download_recording
from .flv import LEGACY_AUDIO, LEGACY_VIDEO
from .index import parse_events
from .media import FFmpegMissing, ensure_ffmpeg, make_speed_variant, render_audio, render_video
from .player import PlayerSources, write_player
from .urls import InvalidLink, parse_link

log = logging.getLogger("connect-dl")

# Connect's API says only "no-access" whether you are anonymous, signed in
# without rights, or holding an expired cookie, so spell out the options.
_AUTH_HINT = (
    "  This server does not allow anonymous access to the API.\n"
    "  Sign in with -u/--user EMAIL, or - if your account uses university "
    "SSO/Shibboleth,\n"
    "  log into Connect in a browser and pass the BREEZESESSION cookie with "
    "--session\n"
    "  (developer tools -> Application -> Cookies)."
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="connect-dl",
        description="Download an Adobe Connect class recording and rebuild it "
                    "into files you can actually play - including a single "
                    "continuous audio track of the lecturer's voice.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=EPILOG,
    )
    parser.add_argument("--version", action="version", version=f"connect-dl {__version__}")
    parser.add_argument("-v", "--verbose", action="count", default=0, help="repeat for more detail")

    sub = parser.add_subparsers(dest="command", required=True)

    def auth_options(p: argparse.ArgumentParser) -> None:
        g = p.add_argument_group("authentication")
        g.add_argument("-u", "--user", help="Adobe Connect login (email)")
        g.add_argument("-p", "--password",
                       help="password; prompted for if omitted, or set CONNECT_PASSWORD")
        g.add_argument("--session",
                       help="a BREEZESESSION cookie copied from a logged-in browser. "
                            "Use this when the account signs in through SSO.")
        g.add_argument("--insecure", action="store_true",
                       help="skip TLS certificate checks (for servers behind "
                            "an intercepting proxy)")

    get = sub.add_parser("get", help="download and rebuild a recording")
    get.add_argument("url", help="recording or meeting-room link")
    auth_options(get)
    _output_options(get)
    get.add_argument("--keep-source", action="store_true",
                     help="keep the downloaded zip after unpacking")
    get.add_argument("--poll-timeout", type=int, default=1800, metavar="SEC",
                     help="how long to wait for the server to build the archive (default: 1800)")

    rebuild = sub.add_parser("rebuild",
                             help="rebuild outputs from an already-downloaded archive")
    rebuild.add_argument("path", type=Path, help="a recording .zip or an unpacked folder")
    _output_options(rebuild)

    inspect = sub.add_parser("inspect", help="show what a downloaded archive contains")
    inspect.add_argument("path", type=Path)

    listing = sub.add_parser("list", help="list the recordings in a room or folder")
    listing.add_argument("url")
    auth_options(listing)

    return parser


def _output_options(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("output")
    g.add_argument("-o", "--out", type=Path, default=Path("."), metavar="DIR",
                   help="where to write results (default: current directory)")
    g.add_argument("--audio-only", action="store_true",
                   help="skip video rendering; produce just the lecture audio (much faster)")
    g.add_argument("--no-audio", action="store_true", help="skip the audio mixdown")
    g.add_argument("--audio-format", default="mp3", choices=("mp3", "m4a", "opus", "wav"))
    g.add_argument("--speed", type=float, metavar="N",
                   help="also write a permanently sped-up audio copy, e.g. 2 or 2.5. "
                        "Only needed for players with no speed control - the "
                        "generated page can already play at any rate.")
    g.add_argument("--no-player", action="store_true", help="do not generate the HTML player")
    g.add_argument("--no-camera", action="store_true",
                   help="leave the webcam out of the rendered video")
    g.add_argument("--width", type=int, default=1280, help="video width (default: 1280)")
    g.add_argument("--height", type=int, default=720, help="video height (default: 720)")
    g.add_argument("--fps", type=int, default=5,
                   help="video frame rate; slides need very few (default: 5)")
    g.add_argument("--offset", action="append", default=[], metavar="NAME=MS",
                   help="override a stream's start time, e.g. cameraVoip_1_2=2670000. "
                        "Repeatable.")
    g.add_argument("--dry-run", action="store_true",
                   help="print the ffmpeg commands instead of running them")


EPILOG = """
examples:
  # list what is available in a class room
  connect-dl list https://connect.example.edu/l993retztu2a/ -u me@example.edu

  # download one recording and rebuild everything
  connect-dl get https://connect.example.edu/p8fj3k2la9x/ -u me@example.edu -o ./class07

  # just the lecturer's voice, as fast as possible
  connect-dl get https://connect.example.edu/p8fj3k2la9x/ --session BREEZE... --audio-only

  # the streams are misaligned - nudge one and rebuild without downloading again
  connect-dl rebuild ./class07/source --offset cameraVoip_1_2=2670000
"""


# ------------------------------------------------------------------ commands

def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=[logging.WARNING, logging.INFO, logging.DEBUG][min(args.verbose, 2)],
        format="%(levelname)s: %(message)s",
    )
    try:
        return {
            "get": cmd_get, "rebuild": cmd_rebuild,
            "inspect": cmd_inspect, "list": cmd_list,
        }[args.command](args)
    except (ConnectError, InvalidLink, FFmpegMissing, RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


def _client(args) -> tuple[ConnectClient, object]:
    link = parse_link(args.url)
    client = ConnectClient(origin=link.origin, verify_tls=not args.insecure)

    session = args.session or link.session or os.environ.get("CONNECT_SESSION")
    if session:
        client.set_session(session)
        who = client.check_session()
        if not who:
            # An expired or mistyped cookie otherwise behaves exactly like
            # passing nothing at all, and the run fails much later with a
            # generic "not public" message that blames the recording.
            raise AuthError(
                "the BREEZESESSION value supplied is not a valid logged-in "
                "session (the server treats it as anonymous). It has probably "
                "expired - Connect sessions are short-lived. Log in again in a "
                "browser and copy a fresh BREEZESESSION cookie."
            )
        print(f"signed in as {who}")
    elif args.user:
        password = args.password or os.environ.get("CONNECT_PASSWORD")
        if not password:
            password = getpass.getpass(f"Password for {args.user}: ")
        client.login(args.user, password)
    else:
        log.info("no credentials given; trying anonymously")
    return client, link


def cmd_list(args) -> int:
    client, link = _client(args)
    if not link.url_path and not link.sco_id:
        raise ConnectError("that link does not identify a room or folder")

    try:
        sco = (client.sco_info(link.sco_id) if link.sco_id
               else client.resolve_url_path(link.url_path))
    except AuthError as exc:
        raise AuthError(f"{exc}\n{_AUTH_HINT}") from exc
    sco_id = sco.get("sco-id", "")
    recordings = client.meeting_recordings(sco_id)

    if not recordings:
        print("no recordings found. The room may have none, or this account may "
              "not be allowed to see them.")
        return 1

    print(f"{len(recordings)} recording(s):\n")
    for rec in recordings:
        print(f"  {rec}")
        print(f"     {rec.link(link.origin).base}")
    return 0


def cmd_get(args) -> int:
    client, link = _client(args)
    out_dir: Path = args.out
    source_dir = out_dir / "source"
    out_dir.mkdir(parents=True, exist_ok=True)

    # A room link has no recording of its own; ask which archive is meant.
    if link.url_path:
        try:
            link = _resolve_room(client, link)
        except AuthError as exc:
            # Anonymous or restricted accounts cannot call the API; the direct
            # asset download sometimes still works for public recordings, so
            # carry on - but say plainly what could not be checked, because the
            # download is about to fail for the same reason.
            print(f"note: cannot identify {link.base} - {exc}")
            print("      so it is unknown whether this is a recording or a "
                  "meeting room.")
            print(_AUTH_HINT + "\n")
        except ConnectError as exc:
            log.info("could not query the API (%s); trying the link directly", exc)

    print(f"downloading {link.base}")
    result = download_recording(
        client, link, out_dir, poll_timeout=args.poll_timeout,
    )

    if not result.is_zip:
        print(f"\nthe server provided a finished video: {result.path}")
        print("nothing to reconstruct - open it in any player and set the speed there.")
        return 0

    archive = unpack(result.path, source_dir)
    if not args.keep_source:
        result.path.unlink(missing_ok=True)

    return _produce(archive, args, out_dir)


def cmd_rebuild(args) -> int:
    path: Path = args.path
    if not path.exists():
        raise ConnectError(f"{path} does not exist")
    archive = unpack(path, path.parent / "source")
    return _produce(archive, args, args.out)


def cmd_inspect(args) -> int:
    path: Path = args.path
    archive = unpack(path, path.parent / "source")
    print(archive.summary())
    events = parse_events(archive.xml_files, duration_ms=archive.duration_ms)
    print(f"\nchapters: {len(events.markers)}   chat lines: {len(events.chat)}")
    return 0


# ------------------------------------------------------------------ pipeline

# Connect labels every SCO with an icon.  A recording is "archive"; the things
# a user is most likely to paste by mistake are rooms and folders, which have no
# downloadable payload of their own.
_CONTAINER_ICONS = {
    "meeting": "meeting room",
    "folder": "folder",
    "curriculum": "curriculum",
    "event": "event",
}


def _resolve_room(client: ConnectClient, link):
    """Turn a room/folder link into a recording link, saying so out loud.

    Raises ConnectError with an explicit explanation when the link identifies a
    container that holds no recordings - previously this fell through to the
    downloader, which polled a login page for half an hour.
    """
    sco = client.resolve_url_path(link.url_path)
    icon = (sco.get("icon") or sco.findtext("icon") or "").strip()
    kind = _CONTAINER_ICONS.get(icon)
    if kind is None:
        return link  # an archive (or something else with its own payload)

    name = sco.findtext("name") or link.url_path
    print(f"{link.base} is a {kind} ({name!r}), not a recording.")

    recordings = client.meeting_recordings(sco.get("sco-id", ""))
    if not recordings:
        raise ConnectError(
            f"that link is a {kind}, not a recording, and it contains no "
            "recordings this account can see. Open the room in a browser, find "
            "the recording you want, and pass its link instead."
        )

    print(f"it contains {len(recordings)} recording(s).\n")
    return _choose(recordings, link)


def _choose(recordings: list[Recording], link):
    if not recordings:
        return link
    if len(recordings) == 1:
        log.info("using the room's only recording: %s", recordings[0].name)
        return recordings[0].link(link.origin)
    print(f"\nthat link is a meeting room with {len(recordings)} recordings:\n")
    for i, rec in enumerate(recordings, 1):
        print(f"  [{i}] {rec}")
    while True:
        answer = input("\nwhich one? (number, or 'q') ").strip()
        if answer.lower() in ("q", "quit", ""):
            raise SystemExit(0)
        if answer.isdigit() and 1 <= int(answer) <= len(recordings):
            return recordings[int(answer) - 1].link(link.origin)
        print("not a valid choice")


def _apply_offsets(archive: RecordingArchive, overrides: list[str]) -> None:
    for item in overrides:
        if "=" not in item:
            raise ValueError(f"--offset expects NAME=MS, got {item!r}")
        name, _, value = item.partition("=")
        try:
            offset = int(value)
        except ValueError:
            raise ValueError(f"--offset {name}: {value!r} is not a whole number of milliseconds")
        matched = [s for s in archive.streams if name.lower() in s.name.lower()]
        if not matched:
            raise ValueError(f"--offset {name}: no stream matches that name")
        for stream in matched:
            stream.offset_ms = offset
            stream.offset_source = "manual"
            log.info("%s moved to %.1fs", stream.name, offset / 1000)


def _produce(archive: RecordingArchive, args, out_dir: Path) -> int:
    _apply_offsets(archive, args.offset)
    out_dir.mkdir(parents=True, exist_ok=True)

    print()
    print(archive.summary())
    print()

    stem = _safe_name(archive.title) or "lecture"
    notes = _notes(archive)
    audio_path = video_path = None

    if not args.dry_run:
        try:
            ensure_ffmpeg()
        except FFmpegMissing as exc:
            print(f"\n{exc}\n")
            print(f"The raw streams are in {archive.root} - install ffmpeg and run:")
            print(f"  connect-dl rebuild {archive.root}")
            return 1

    # Audio first: it is the deliverable that must not fail, and it is quick.
    if not args.no_audio and archive.audio_streams:
        audio_path = out_dir / f"{stem}.{args.audio_format}"
        render_audio(archive, audio_path, fmt=args.audio_format, dry_run=args.dry_run)
        print(f"  audio  -> {audio_path}")

        if args.speed:
            fast = out_dir / f"{stem}-{args.speed:g}x.{args.audio_format}"
            make_speed_variant(audio_path, fast, args.speed, dry_run=args.dry_run)
            print(f"  audio  -> {fast}  ({args.speed:g}x)")
    elif not archive.audio_streams:
        print("  !! no audio stream in this recording - skipping the audio mixdown")

    if not args.audio_only and archive.video_streams:
        video_path = out_dir / f"{stem}.mp4"
        try:
            render_video(
                archive, video_path,
                width=args.width, height=args.height, fps=args.fps,
                camera_pip=not args.no_camera, dry_run=args.dry_run,
            )
            print(f"  video  -> {video_path}")
        except RuntimeError as exc:
            # Video reconstruction is the fragile half; never let it take the
            # audio down with it.
            log.error("video rendering failed: %s", exc)
            notes.append("Video rendering failed; the audio track is unaffected.")
            video_path = None

    if not args.no_player and not args.dry_run and (audio_path or video_path):
        events = parse_events(archive.xml_files, duration_ms=archive.duration_ms)
        page = write_player(
            out_dir / "play.html",
            title=archive.title or stem,
            sources=PlayerSources(
                video=video_path.name if video_path else None,
                audio=audio_path.name if audio_path else None,
            ),
            events=events,
            duration_s=archive.duration_ms / 1000.0,
            notes=notes,
        )
        print(f"  player -> {page}")

    print("\ndone.")
    if notes:
        print("\nnotes:")
        for note in notes:
            print(f"  - {note}")
    return 0


def _notes(archive: RecordingArchive) -> list[str]:
    notes: list[str] = []
    guessed = [s for s in archive.streams
               if s.offset_source in ("sequential", "assumed-zero") and s.duration_ms > 0]
    if guessed:
        names = ", ".join(s.name for s in guessed[:4])
        notes.append(
            f"Start times for {names} were guessed rather than read from the "
            f"recording; if those parts drift out of sync, correct them with "
            f"--offset NAME=MILLISECONDS and run 'connect-dl rebuild'."
        )
    legacy = {s.info.audio_codec for s in archive.streams
              if s.info and s.info.audio_codec in LEGACY_AUDIO}
    legacy |= {s.info.video_codec for s in archive.streams
               if s.info and s.info.video_codec in LEGACY_VIDEO}
    if legacy:
        notes.append(
            "This is a Flash-era recording (" + ", ".join(sorted(legacy)) +
            "). Those codecs were re-encoded so ordinary players can read them."
        )
    if not archive.by_role(Role.SCREENSHARE) and archive.by_role(Role.MAIN):
        notes.append(
            "No separate screen-share stream was present, so the server's own "
            "composite (mainstream) was used for the picture."
        )
    return notes


def _safe_name(title: str | None) -> str | None:
    if not title:
        return None
    cleaned = re.sub(r"[^\w؀-ۿ .-]+", "", title, flags=re.UNICODE).strip()
    cleaned = re.sub(r"\s+", "-", cleaned)
    return cleaned[:80] or None


# `python3 -m connect_dl` goes through __main__.py; running this module directly
# as `python3 -m connect_dl.cli` executes it with __name__ == "__main__" and
# would otherwise define main() and exit 0 without ever calling it.
if __name__ == "__main__":
    sys.exit(main())

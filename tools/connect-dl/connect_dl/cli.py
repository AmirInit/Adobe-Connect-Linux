"""Command line entry point."""

from __future__ import annotations

import argparse
import getpass
import logging
import os
import re
import sys
import webbrowser
from pathlib import Path

from . import __version__
from .api import AuthError, ConnectClient, ConnectError, Recording  # noqa: F401
from .archive import LAYOUTS, RecordingArchive, Role, unpack
from .fetch import download_recording
from .flv import LEGACY_AUDIO, LEGACY_VIDEO
from .index import parse_events
from .levels import level_streams, quiet_report
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
                    "into files you can actually play - a single continuous "
                    "audio track of the lecturer's voice, the reconstructed "
                    "video, and an offline player that opens itself.",
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
    # Optional on purpose: `connect-dl get` with no arguments at all should ask
    # for the link and then do everything else by itself.
    get.add_argument("url", nargs="?",
                     help="any Connect link - recording or meeting room, with or "
                          "without a ?session= token. Asked for if omitted.")
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
    inspect.add_argument("--layout", choices=LAYOUTS, default="auto",
                         help="see how a different alignment would place the segments")
    inspect.add_argument("--no-level", action="store_true",
                         help="skip measuring microphone levels (faster)")

    listing = sub.add_parser("list", help="list the recordings in a room or folder")
    listing.add_argument("url", nargs="?", help="a meeting-room or folder link")
    auth_options(listing)

    play = sub.add_parser("play", help="open a rebuilt recording's player in your browser")
    play.add_argument("path", type=Path, nargs="?", default=Path("."),
                      help="the output folder (or the play.html itself); defaults to here")

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
    g.add_argument("--no-open", action="store_true",
                   help="do not open the player in a browser when the rebuild finishes")
    g.add_argument("--only-speaker", type=int, metavar="N",
                   help="keep just one speaker's microphone; run 'inspect' to see who is who")
    g.add_argument("--layout", choices=LAYOUTS, default="auto",
                   help="how to place segments in time. 'timeline' trusts the "
                        "recorded start times, 'sequential' ignores them and lays "
                        "the segments end to end (default: auto)")
    g.add_argument("--no-level", action="store_true",
                   help="do not equalise the loudness of different microphones "
                        "(levelling is on by default)")
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
the short version:
  connect-dl get                      asks for the link, then does everything
  connect-dl get "<paste the link>"   same, with the link already in hand

Either one downloads the recording, rebuilds the audio and video, writes the
chat and chapters beside them, and opens the player in your browser.  Every
flag below is optional.

more examples:
  # what recordings does this class room have?
  connect-dl list https://connect.example.edu/l993retztu2a/

  # somewhere other than here, and just the voice (much faster)
  connect-dl get "https://connect.example.edu/p8fj3k2la9x/?session=abc" -o ./class07 --audio-only

  # everybody is talking at once - lay the segments end to end instead
  connect-dl rebuild ./class07/source -o ./class07 --layout sequential

  # keep only the lecturer's microphone (inspect shows who is who)
  connect-dl inspect ./class07/source
  connect-dl rebuild ./class07/source -o ./class07 --only-speaker 1

  # watch it again later
  connect-dl play ./class07
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
            "get": cmd_get, "rebuild": cmd_rebuild, "inspect": cmd_inspect,
            "list": cmd_list, "play": cmd_play,
        }[args.command](args)
    except (ConnectError, InvalidLink, FFmpegMissing, RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


def _ask_link(raw: str | None) -> str:
    """The link, asked for when it was not given."""
    if raw and raw.strip():
        return raw.strip()
    if not sys.stdin.isatty():
        raise InvalidLink(
            "no link given. Pass one on the command line: "
            "connect-dl get https://connect.example.edu/abc123/"
        )
    answer = input("Connect link: ").strip()
    if not answer:
        raise InvalidLink("no link given")
    return answer


def _ask_session(args, link) -> str | None:
    """Find a BREEZESESSION, in the order that costs the user the least.

    A link pasted out of the browser usually carries ``?session=…`` already -
    that token does not authenticate as a query parameter, but it is the right
    value to send as the Cookie header, so it is used rather than asked for.
    """
    if args.session:
        return args.session
    if link.session:
        print("using the session token found in the link")
        return link.session
    from_env = os.environ.get("CONNECT_SESSION")
    if from_env:
        return from_env
    if args.user or not sys.stdin.isatty():
        return None
    # Not echoed: it is a live credential for the whole Connect account.
    entered = getpass.getpass("BREEZESESSION cookie (blank if public): ").strip()
    return entered or None


def _client(args) -> tuple[ConnectClient, object]:
    link = parse_link(_ask_link(getattr(args, "url", None)))
    client = ConnectClient(origin=link.origin, verify_tls=not args.insecure)

    session = _ask_session(args, link)
    if session:
        client.set_session(session)
        try:
            who = client.check_session()
        except ConnectError as exc:
            # The XML API being unreachable or closed to this account says
            # nothing about the session itself, and the asset download does not
            # go through the API - so this is a note, not a failure.
            log.info("could not confirm the session through the API (%s)", exc)
            who = "(unverified)"
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

    archive = unpack(result.path, source_dir, layout=args.layout)
    if not args.keep_source:
        result.path.unlink(missing_ok=True)

    return _produce(archive, args, out_dir)


def cmd_rebuild(args) -> int:
    path: Path = args.path
    if not path.exists():
        raise ConnectError(f"{path} does not exist")
    archive = unpack(path, path.parent / "source", layout=args.layout)
    return _produce(archive, args, args.out)


def cmd_inspect(args) -> int:
    """Report what is in an archive - timeline, speakers, quiet mics - and stop."""
    path: Path = args.path
    archive = unpack(path, path.parent / "source", layout=args.layout)

    raised = [] if args.no_level else _level(archive)
    print(archive.summary())

    totals = archive.speaker_totals()
    if len(totals) > 1:
        print("\nspeakers, by microphone time:")
        for speaker, ms in sorted(totals.items(), key=lambda kv: -kv[1]):
            print(f"  speaker {speaker}: {ms / 60000:5.1f} min")
        print("  the lecturer is usually the one with the most; keep only them")
        print("  with --only-speaker N")

    if raised:
        print()
        for line in quiet_report(raised):
            print(line)

    events = parse_events(archive.xml_files, duration_ms=archive.duration_ms)
    print(f"\nchapters: {len(events.markers)}   chat lines: {len(events.chat)}")
    return 0


def cmd_play(args) -> int:
    """Open a previously rebuilt recording in the browser."""
    page = find_player(args.path)
    if page is None:
        raise ConnectError(
            f"no play.html under {args.path}. Rebuild the recording first:\n"
            f"  connect-dl rebuild {args.path}"
        )
    print(f"opening {page}")
    if not open_in_browser(page):
        print("could not launch a browser; open that file yourself.")
    return 0


def find_player(path: Path) -> Path | None:
    """Locate the play.html for a folder (or accept the file itself)."""
    path = path.expanduser()
    if path.is_file() and path.suffix.lower() in (".html", ".htm"):
        return path
    direct = path / "play.html"
    if direct.is_file():
        return direct
    found = sorted(path.glob("*/play.html")) if path.is_dir() else []
    return found[0] if found else None


def open_in_browser(page: Path) -> bool:
    """Open a local file in the default browser.  False if that is impossible.

    Headless machines, ssh sessions and CI have no browser to open, and that is
    not an error worth failing a finished download over.
    """
    if os.environ.get("CONNECT_DL_NO_BROWSER"):
        return False
    try:
        return webbrowser.open(page.resolve().as_uri())
    except (webbrowser.Error, OSError, ValueError) as exc:
        log.debug("could not open a browser: %s", exc)
        return False


def _level(archive: RecordingArchive) -> list:
    """Measure the microphones, unless ffmpeg is missing (then skip quietly)."""
    try:
        tool = ensure_ffmpeg()
    except FFmpegMissing:
        log.info("ffmpeg is not installed, so microphone levels cannot be measured")
        return []
    print("measuring microphone levels (one pass per segment; --no-level skips this)")
    return level_streams(archive.audio_streams, ffmpeg=tool.ffmpeg)


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
    """Offer the recordings inside a room link - but never insist on it.

    A room handle is not a dead end: on the servers this tool is used against,
    ``<room>/output/class.zip`` downloads the room's recording directly and no
    separate recording handle exists at all.  So this looks for recordings to
    choose from and, when there are none to offer, hands the original link back
    for the downloader to try.  (It used to raise here, which turned a link that
    would have worked into a hard failure.)
    """
    sco = client.resolve_url_path(link.url_path)
    icon = (sco.get("icon") or sco.findtext("icon") or "").strip()
    kind = _CONTAINER_ICONS.get(icon)
    if kind is None:
        return link  # an archive (or something else with its own payload)

    name = sco.findtext("name") or link.url_path
    recordings = client.meeting_recordings(sco.get("sco-id", ""))
    if not recordings:
        log.info("%s is a %s (%r) with no separately listed recordings; "
                 "downloading from the room handle itself", link.base, kind, name)
        return link

    print(f"{link.base} is a {kind} ({name!r}) holding {len(recordings)} recording(s).")
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

    if not args.dry_run:
        try:
            ensure_ffmpeg()
        except FFmpegMissing as exc:
            print(f"\n{exc}\n")
            print(f"The raw streams are in {archive.root} - install ffmpeg and run:")
            print(f"  connect-dl rebuild {archive.root}")
            return 1

    audio = archive.audio_for(args.only_speaker)
    if args.only_speaker is not None and not audio:
        raise ConnectError(
            f"no microphone segments belong to speaker {args.only_speaker}. "
            "Run 'connect-dl inspect' on the source folder to see which speakers "
            "this recording has."
        )

    raised = []
    if not args.no_level and audio and not args.dry_run:
        raised = _level(archive)

    print()
    print(archive.summary())
    if raised:
        print()
        for line in quiet_report(raised):
            print(line)
    print()

    stem = _safe_name(archive.title) or "lecture"
    notes = _notes(archive, raised)
    audio_path = video_path = None

    # Audio first: it is the deliverable that must not fail, and it is quick.
    if not args.no_audio and audio:
        audio_path = out_dir / f"{stem}.{args.audio_format}"
        render_audio(archive, audio_path, fmt=args.audio_format, streams=audio,
                     dry_run=args.dry_run)
        print(f"  audio  -> {audio_path}")

        if args.speed:
            fast = out_dir / f"{stem}-{args.speed:g}x.{args.audio_format}"
            make_speed_variant(audio_path, fast, args.speed, dry_run=args.dry_run)
            print(f"  audio  -> {fast}  ({args.speed:g}x)")
    elif not audio:
        print("  !! no audio stream in this recording - skipping the audio mixdown")
        print("     if the class used a telephone bridge, its audio is not in the")
        print("     archive at all and only a Connect administrator can retrieve it.")

    if not args.audio_only and archive.video_streams:
        video_path = out_dir / f"{stem}.mp4"
        try:
            render_video(
                archive, video_path,
                width=args.width, height=args.height, fps=args.fps,
                camera_pip=not args.no_camera, audio=audio, dry_run=args.dry_run,
            )
            print(f"  video  -> {video_path}")
        except RuntimeError as exc:
            # Video reconstruction is the fragile half; never let it take the
            # audio down with it.
            log.error("video rendering failed: %s", exc)
            notes.append("Video rendering failed; the audio track is unaffected.")
            video_path = None

    events = parse_events(archive.xml_files, duration_ms=archive.duration_ms)
    if not args.dry_run:
        for written in _write_sidecars(events, out_dir):
            print(f"  text   -> {written}")

    page = None
    if not args.no_player and not args.dry_run and (audio_path or video_path):
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

    # One command, paste a link, watch the class: the last step of a successful
    # run is the player already being on screen.
    if page and not args.no_open:
        if open_in_browser(page):
            print(f"\nopened {page.name} in your browser.")
        else:
            print(f"\nopen {page} in a browser to watch it "
                  f"(or run: connect-dl play {out_dir})")
    return 0


def _write_sidecars(events, out_dir: Path) -> list[Path]:
    """Write the chat and chapter list as plain text beside the media.

    The player embeds both, but a text file is greppable, quotable and outlives
    any browser - so they are written as separate files as well.
    """
    written: list[Path] = []
    if events.chat:
        path = out_dir / "chat.txt"
        path.write_text(
            "\n".join(
                f"[{_clock(c.time_ms)}] {c.sender + ': ' if c.sender else ''}{c.message}"
                for c in events.chat
            ) + "\n",
            encoding="utf-8",
        )
        written.append(path)
    if events.markers:
        path = out_dir / "chapters.txt"
        path.write_text(
            "\n".join(f"[{_clock(m.time_ms)}] {m.label}" for m in events.markers) + "\n",
            encoding="utf-8",
        )
        written.append(path)
    return written


def _clock(ms: int) -> str:
    seconds = max(0, ms) // 1000
    return f"{seconds // 3600:d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


def _notes(archive: RecordingArchive, raised: list | None = None) -> list[str]:
    notes: list[str] = []
    if archive.layout == "sequential":
        notes.append(
            "The recorded start times were start-up jitter rather than positions "
            "in the meeting, so the segments were laid end to end in recorded "
            "order. If that sounds out of order, rebuild with --layout timeline."
        )
    guessed = [s for s in archive.streams
               if s.offset_source in ("sequential", "assumed-zero") and s.duration_ms > 0]
    if guessed and archive.layout != "sequential":
        names = ", ".join(s.name for s in guessed[:4])
        notes.append(
            f"Start times for {names} were guessed rather than read from the "
            f"recording; if those parts drift out of sync, correct them with "
            f"--offset NAME=MILLISECONDS and run 'connect-dl rebuild'."
        )
    if raised:
        loudest = max(raised, key=lambda s: s.gain_db)
        notes.append(
            f"{len(raised)} quiet microphone(s) were raised to be audible "
            f"(up to +{loudest.gain_db:.0f} dB on {loudest.name}). Use --no-level "
            "to keep the original levels."
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

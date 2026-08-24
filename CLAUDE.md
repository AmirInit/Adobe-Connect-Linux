# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repository is

Two mostly-independent things live here:

1. **The `connect` desktop client** (C++, repo root) — an unofficial Linux client for Adobe
   Connect meetings. It wraps a CEF (Chromium Embedded Framework) browser with a PPAPI Flash
   plugin, because Adobe Connect's classic meeting room is a Flash application.
2. **`tools/connect-dl`** (Python, stdlib-only) — downloads a *recorded* Adobe Connect class
   and rebuilds it into a playable `.mp3`/`.mp4` plus an offline HTML player. Unrelated to CEF
   or Flash; it just talks to Connect's HTTP API and shells out to `ffmpeg`.

These have separate build/test workflows — see below.

## The `connect` client (C++)

### Build

```bash
./make.sh          # downloads CEF 86 binaries, builds libcef_dll_wrapper, compiles main.cpp+MyApp.cpp
cd Release
./connect "connectpro://YOUR_MEETING_URL"
```

`make.sh` is the only supported build path — it fetches the pinned CEF binary distribution,
builds `libcef_dll_wrapper`, links everything, then stages a self-contained app directory under
`Release/` (binary + `libcef.so` + CEF resources + `install.sh`/`open.sh`/icon). There is no
separate incremental build; re-run `make.sh` after source changes. The VS Code task
(`.vscode/tasks.json`) does a lighter ad-hoc compile to `connect-test` for IntelliSense/debugging
only — it does not stage a runnable app dir.

### Install

```bash
./install.sh       # user-space install to $HOME/.local/share/adobe-connect, no sudo
```

Registers the `connectpro://` URL scheme handler and downloads a patched Flash PPAPI plugin to
`$HOME/.local/lib/flash`. Safe to re-run with a different `INSTALL_DIRECTORY` env var — it
generates `open.sh`/`connect.desktop` from templates rather than editing files in place.

### CEF version is pinned — do not upgrade casually

CEF is pinned to **86** (Chromium 86) because Chromium removed the PPAPI Flash interface in
CEF 88+, and the meeting room is a Flash app. Bumping the CEF version will build fine and then
fail to load any meeting. This only affects the live-meeting client — `tools/connect-dl` has no
CEF/Flash dependency at all.

### Architecture

- `main.cpp` — entry point. Parses the `connectpro://` URL, opens an X11 window (via raw
  Xlib — no toolkit), creates a CEF browser as a child of that window, and runs a single-threaded
  loop that both pumps X11 events (`pump_x_events`) and drives CEF's message loop
  (`CefDoMessageLoopWork`) at ~60Hz. Deliberately single-threaded: an earlier version pumped X11
  on a second thread without `XInitThreads`, which raced against CEF's own X11 use and crashed on
  shutdown.
- `MyApp.{h,cpp}` — `CefApp`/`CefRenderProcessHandler` implementation. Its main job is
  `OnBeforeCommandLineProcessing`, which injects the Flash plugin path/version and permissive
  flags (`allow-outdated-plugins`, `plugin-policy=allow`, `ignore-certificate-errors`, etc.)
  before CEF's subprocesses spawn.
- `MyClient.h` — minimal `CefClient`/`CefLifeSpanHandler`. `GetLifeSpanHandler()` must return
  `this` or the lifespan callbacks are silently never delivered (CefClient's default is null).
- CEF sub-processes share the same `connect` executable (`CefExecuteProcess` at the top of
  `main()` detects and handles this — the rest of `main()` only runs in the browser process).
- The CEF SDK itself lives in `include/` and the `CEF` submodule; both are vendored/generated,
  not hand-edited.

## `tools/connect-dl` (Python)

### Run

```bash
./tools/connect-dl/connect-dl get                       # asks for the link, does the rest
./tools/connect-dl/connect-dl get "https://connect.example.edu/abc123/?session=TOKEN"
./tools/connect-dl/connect-dl list https://connect.example.edu/l993retztu2a/
./tools/connect-dl/connect-dl inspect ./out/source
./tools/connect-dl/connect-dl rebuild ./out/source -o ./out --offset cameraVoip_1_2=2670000
./tools/connect-dl/connect-dl play ./out
```

No install step needed (`./connect-dl` self-inserts its own directory onto `sys.path`); it can
also be run as `python3 -m connect_dl` from within `tools/connect-dl`. Standard library only —
the one external runtime dependency is `ffmpeg` on `PATH`.

No flag is mandatory: a missing link is prompted for, a missing session is taken from the link's
`?session=` or prompted for without echoing, and a successful run opens `play.html` in the
browser (`--no-open`, or `CONNECT_DL_NO_BROWSER=1`, suppresses that — set it when scripting).

### Test

Tests must be run from `tools/connect-dl/` — `tests/` has no `__init__.py`, so pointing
discovery at it from the repo root fails with `Start directory is not importable`:

```bash
cd tools/connect-dl
python3 -m unittest discover tests          # 64 tests
python3 -m unittest tests.test_connect_dl.TestUrls.test_recording_link   # a single test
```

The suite needs no ffmpeg and no network: it builds its own FLV files and serves
its own fake responses. CI (`.github/workflows/connect-dl-tests.yml`) runs it on
3.9 and 3.13 for changes under `tools/connect-dl/` only — it never builds the
C++ client.

### Pipeline architecture

The tool models a recording as: **fetch → identify → align → render**, implemented across:

- `urls.py` — parses the various link shapes a user might paste (recording link, meeting-room
  link, `connectpro://` URL) into a normalized form.
- `api.py` — `ConnectClient`: the Connect XML login/session API (password login, `--session`
  cookie auth, SCO/room resolution, listing recordings).
- `fetch.py` — requests the server build a downloadable zip and polls the holding page
  until it's ready; falls back to taking a finished MP4 only *after* every zip candidate.
  `output/class.zip?download=zip` is the primary candidate — the name real servers serve.
  **The order is load-bearing**: `output/recording.mp4` answers with the login page on a
  server that does not have it, and a login page is terminal, so probing mp4 first aborted
  the run before the working zip URL was ever requested. Downloads resume from bytes on
  disk with a Range header (large archives drop mid-transfer), restart cleanly when the
  server ignores the range or answers 416, and are verified with `zip_is_complete()`
  before being trusted.
- `archive.py` — `unpack()`/`RecordingArchive`: extracts the zip and classifies each file into a
  `Stream` with a `Role` (screenshare, camera, voip audio, etc.).
- `flv.py` — a from-scratch FLV tag-header parser (fast — does not decode payloads) used to read
  each stream's real codec and timestamp span, since Connect's own metadata about a stream is
  frequently missing or wrong.
- **Alignment** (in `archive.py`/`cli.py`): every stream needs a position on one shared timeline.
  Each stream's `offset_source` is one of `xml` (stated in metadata), `flv-timestamp` (read from
  the container) — both trusted — or `sequential`/`assumed-zero` — laid end to end. This is a
  whole-archive decision, not per-stream, because getting it wrong reorders halves of the lecture
  rather than causing a subtle drift.

  **"Any non-zero timestamp means real positions" is wrong** and was the bug: on a real 38-minute
  recording six microphone segments all began within 4.4s of each other — start-up jitter — and
  every one of those starts was non-zero, so all six were stacked on top of each other and
  everybody talked at once. `_timestamps_are_positions()` therefore judges by *magnitude*: the
  spread between the first and last start must reach a quarter of the longest single track
  (`_SPREAD_FRACTION`) and at least 30s (`_MIN_SPREAD_MS`) before the stamps count as positions.
  The xml-hint rule still sits above it. `--layout timeline|sequential` overrides the decision;
  `--offset NAME=MS` (parsed in `cli.py`, `_apply_offsets`) overrides one stream for a `rebuild`
  without re-downloading.
- `levels.py` — per-segment microphone levelling. Every mic in a Connect room has its own gain
  and nothing normalises them; a lecturer's headset and a student on a laptop measured 29 dB
  apart, which is the difference between clear and inaudible and cannot be fixed with the volume
  knob. `plan_gain()` targets −20 dB mean, capped by peak headroom (no clipping) and by
  `MAX_BOOST_DB` (no amplifying room tone into hiss), and never negative. `--no-level` opts out.
  The gain is applied in `media.audio_branch()` *before* `adelay`, so the padding stays silence.
- **Metadata is not media.** `ftchat*`, `ftcontent*`, `indexstream*`, `transcriptstream*` (and
  the rest of `archive.METADATA_PREFIXES`) are FLV containers holding chat, whiteboard
  draw-commands and the seek index. `_classify()` maps them to `Role.METADATA` **by name, before
  the container is consulted**, and they are never selected as a render input — feeding one to
  ffmpeg is what makes a render "succeed" while producing the wrong thing.
- `index.py` — parses the recording's XML event stream into chapter markers and chat transcript
  (`parse_events`).
- `media.py` — builds and runs the `ffmpeg` filter graphs: `build_audio_filter`/`render_audio`
  (delay + mix each audio segment onto its true offset), `build_video_filter`/`render_video`
  (screenshare + webcam PIP composite), `atempo_chain` (chains `atempo` filters since ffmpeg caps
  a single one at 2x, for `--speed` and player-side rate control), `ensure_ffmpeg`/
  `FFmpegMissing`.
- `player.py` — `write_player()` generates the self-contained offline `play.html` (speed control
  with pitch correction, chapters, chat sidebar).
- `cli.py` — argparse subcommands (`get`, `rebuild`, `inspect`, `list`, `play`) that wire the
  above together; `_produce()` in particular is the render pipeline every `get`/`rebuild` goes
  through (audio first since it must not fail, then video best-effort, then the sidecar
  chat/chapter text, then the player, then opening it in a browser).

There is a second, deliberate copy of all this at `standalone/ac_downloader.py`: the whole tool
in one file with no package to install. It is *not* generated from the package — it is a parallel
implementation — so `TestTheTwoCopiesAgree` pins the decisions where disagreement would silently
produce a different recording (link parsing, metadata prefixes, the alignment threshold, the gain
policy, login-page detection). Change one of those and change both.

Design invariant worth preserving: audio rendering must never be taken down by a video failure —
`_produce()` catches video `RuntimeError` and downgrades it to a note rather than failing the
whole command.

### How a real Connect server behaves (verified against `vadavc41.ec.iau.ir`, Connect 10.8.0)

Observed behaviour that is easy to get wrong, and that the code now defends against:

- **The asset is `output/class.zip?download=zip`.** `output/recording.mp4` returns the login
  page rather than a 404, which is why zip candidates are tried before mp4 ones.
- **The meeting-room handle downloads directly.** There is no separate recording handle on this
  server, and `sco-contents` lists no recordings for the room. `_resolve_room()` must therefore
  offer what it finds and otherwise hand the link back — it used to raise here, turning a link
  that would have worked into a hard failure.
- **`?session=` in the URL query does not authenticate anything.** The token has to be sent as
  `Cookie: BREEZESESSION=<token>`. The tool lifts it out of the URL and sets the cookie itself,
  which is why pasting the launcher URL works.
- **Auth failures arrive as HTTP 200 + an HTML login page, not 401/403.** Requesting
  `<room>/output/recording.zip?download=zip` unauthenticated returns `Content-Type: text/html`
  with the "Adobe Connect Central Login" page. Nothing but the *body* distinguishes this from the
  legitimate "still building your zip" holding page that the poll loop is designed around.
  `api.looks_like_login_page()` makes that distinction, and `fetch._try_download` raises
  `AuthError` on it. Without this the tool polls a login page for the full 30-minute
  `--poll-timeout`. **Any new response-classification code must keep login pages terminal and
  holding pages retryable** — conflating them in either direction is a bad failure.
- **The XML API refuses anonymous access entirely**: `sco-by-url` returns `no-access`. So for an
  unauthenticated user the tool cannot tell a recording from a meeting room, and must say so
  rather than guessing.
- **`no-access` is returned for every distinct auth problem** — anonymous, signed-in-without-
  rights, and expired-cookie all look identical. Error messages therefore have to enumerate the
  possibilities (`_AUTH_HINT` in `cli.py`) instead of asserting one cause.
- **An expired/invalid `BREEZESESSION` authenticates as anonymous rather than erroring.**
  `check_session()` returning `None` is the only signal, so `_client()` treats a supplied-but-
  anonymous session as a hard error; otherwise a stale cookie is indistinguishable from passing
  no credentials and the run fails much later blaming the recording.
- Connect sessions are short-lived, so `--session` cookies go stale quickly during testing.
- That host's TLS certificate **does** verify normally — `--insecure` is not needed for it,
  despite the C++ client hardcoding `--ignore-certificate-errors`. Do not reach for `--insecure`
  as a first resort.

`ConnectClient._open` retries with exponential backoff, but `_permanent_reason()` classifies
DNS-resolution, certificate-verification and `WRONG_VERSION_NUMBER` (a plain-HTTP answer on a
TLS connection) failures as verdicts and fails immediately — retrying those only multiplies the
wait before the identical error. Note `ssl.SSLError.reason` is a *string*, not an exception, so
that helper inspects both the exception and its `.reason`.

`parse_link()` leaves an explicit `http://` alone. Rewriting it to https (which it used to do)
produced a `WRONG_VERSION_NUMBER` that reads like a broken server rather than like a URL the tool
had changed. Bare hosts and `connectpro://` still mean https, and surrounding quotes, angle
brackets and whitespace are stripped, because that is how links arrive when pasted.

Testing against a real server needs credentials — read them from `CONNECT_SESSION` /
`CONNECT_PASSWORD`, never commit them.

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
./tools/connect-dl/connect-dl get https://connect.example.edu/p8fj3k2la9x/ -u me@example.edu -o ./out
./tools/connect-dl/connect-dl list https://connect.example.edu/l993retztu2a/ -u me@example.edu
./tools/connect-dl/connect-dl inspect ./out/source
./tools/connect-dl/connect-dl rebuild ./out/source -o ./out --offset cameraVoip_1_2=2670000
```

No install step needed (`./connect-dl` self-inserts its own directory onto `sys.path`); it can
also be run as `python3 -m connect_dl` from within `tools/connect-dl`. Standard library only —
the one external runtime dependency is `ffmpeg` on `PATH`.

### Test

Tests must be run from `tools/connect-dl/` — `tests/` has no `__init__.py`, so pointing
discovery at it from the repo root fails with `Start directory is not importable`:

```bash
cd tools/connect-dl
python3 -m unittest discover tests          # 21 tests
python3 -m unittest tests.test_connect_dl.TestUrls.test_recording_link   # a single test
```

### Pipeline architecture

The tool models a recording as: **fetch → identify → align → render**, implemented across:

- `urls.py` — parses the various link shapes a user might paste (recording link, meeting-room
  link, `connectpro://` URL) into a normalized form.
- `api.py` — `ConnectClient`: the Connect XML login/session API (password login, `--session`
  cookie auth, SCO/room resolution, listing recordings).
- `fetch.py` — requests the server build a downloadable zip
  (`<recording-url>/output/…zip?download=zip`) and polls the holding page until it's ready;
  falls back to taking a finished MP4 directly when the HTML client offers one.
- `archive.py` — `unpack()`/`RecordingArchive`: extracts the zip and classifies each file into a
  `Stream` with a `Role` (screenshare, camera, voip audio, etc.).
- `flv.py` — a from-scratch FLV tag-header parser (fast — does not decode payloads) used to read
  each stream's real codec and timestamp span, since Connect's own metadata about a stream is
  frequently missing or wrong.
- **Alignment** (in `archive.py`/`cli.py`): every stream needs a position on one shared timeline.
  Each stream's `offset_source` is one of `xml` (stated in metadata), `flv-timestamp` (read from
  the container) — both trusted — or `sequential`/`assumed-zero` — guessed by laying segments end
  to end. This is a whole-archive decision, not per-stream: one real non-zero timestamp anywhere
  means Connect stamped true meeting positions, so getting it wrong reorders halves of the
  lecture rather than causing a subtle drift. `--offset NAME=MS` (parsed in `cli.py`,
  `_apply_offsets`) manually overrides a stream's offset for a `rebuild` without re-downloading.
- `index.py` — parses the recording's XML event stream into chapter markers and chat transcript
  (`parse_events`).
- `media.py` — builds and runs the `ffmpeg` filter graphs: `build_audio_filter`/`render_audio`
  (delay + mix each audio segment onto its true offset), `build_video_filter`/`render_video`
  (screenshare + webcam PIP composite), `atempo_chain` (chains `atempo` filters since ffmpeg caps
  a single one at 2x, for `--speed` and player-side rate control), `ensure_ffmpeg`/
  `FFmpegMissing`.
- `player.py` — `write_player()` generates the self-contained offline `play.html` (speed control
  with pitch correction, chapters, chat sidebar).
- `cli.py` — argparse subcommands (`get`, `rebuild`, `inspect`, `list`) that wire the above
  together; `_produce()` in particular is the render pipeline every `get`/`rebuild` goes through
  (audio first since it must not fail, then video best-effort, then the player).

Design invariant worth preserving: audio rendering must never be taken down by a video failure —
`_produce()` catches video `RuntimeError` and downgrades it to a note rather than failing the
whole command.

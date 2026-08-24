# connect-dl

Downloads a recorded Adobe Connect class and rebuilds it into files you can
actually play — a single continuous audio track of the lecturer's voice, the
reconstructed video, the chat and chapters, and an offline player that opens
itself.

Adobe Connect does not hand you a video file. A recording is stored as a pile of
separate Flash-era streams — the shared screen, the webcam, each stretch of
microphone audio — with no manifest a normal media player understands, and with
timing information that is frequently missing or wrong. This tool fetches them,
works out where each one belongs on the meeting's timeline, evens out the
microphones, and reassembles the lecture.

---

## This repository contains two independent things

| | What it is | Needs |
|---|---|---|
| **the `connect` client** (repo root, C++) | joins a **live** meeting; wraps CEF 86 + the PPAPI Flash plugin, because the classic meeting room is a Flash application | CEF, Flash |
| **`tools/connect-dl`** (this directory, Python) | downloads a **recorded** class and rebuilds it | Python, ffmpeg |

**They share nothing.** connect-dl does not use CEF, Flash, or any part of the
C++ client; it talks to Connect over ordinary HTTP and shells out to `ffmpeg`.
You can use this tool without ever building the client, on a machine where the
client could not run at all — a Mac, a server, anything. Nothing here is
affected by the client's CEF 86 pin.

---

## Install

There is no install step. You need:

- **Python 3.9 or newer** — standard library only, nothing to `pip install`
- **ffmpeg** — the one external dependency; it is what decodes Connect's
  Nellymoser/Speex audio and Flash Screen Video

```bash
brew install ffmpeg              # macOS
sudo apt install ffmpeg          # Debian/Ubuntu
sudo dnf install ffmpeg          # Fedora
sudo pacman -S ffmpeg            # Arch
```

Then run `./connect-dl` straight out of the checkout.

---

## The one command

```bash
./connect-dl get
```

That is the whole thing. It asks for the link, asks for your session cookie if
the link does not already carry one, downloads the recording, rebuilds it, and
**opens the player in your browser**. If you already have the link in hand:

```bash
./connect-dl get "https://connect.example.edu/l993retztu2a/?session=TOKEN&proto=true"
```

Paste the link exactly as you have it — quotes, `?session=`, `&proto=true` and
all. A recording link, a meeting-room link and the `connectpro://…` URL the
desktop client is launched with all work; so does a bare `host.edu/abc123`.

**No flag is ever required.** Everything below is for when the default is not
what you want.

---

## Getting a BREEZESESSION cookie

Most university recordings are not public, and most university accounts sign in
through SSO — which the Connect XML login API cannot do at all. The way in is
the session cookie from a browser where you are already logged in.

You usually do not need to do this by hand: **if the link you were given
contains `?session=…`, the tool takes the token from there.** (That token does
not authenticate anything as a URL parameter — Connect wants it as a `Cookie`
header — which is why pasting the link into a browser address bar sometimes
works and downloading from it does not. connect-dl sends it correctly.)

When you do need it:

1. Log into Connect in your browser, so you can see the recording.
2. Press <kbd>F12</kbd> to open developer tools.
3. Go to **Application** (Chrome/Edge) or **Storage** (Firefox).
4. In the left sidebar open **Cookies**, then your Connect server's address.
5. Find the row named **`BREEZESESSION`** and copy its **Value** — a long string
   like `breezxc41abcdefghijklmnop`.
6. Pass it:

```bash
./connect-dl get https://connect.example.edu/l993retztu2a/ --session breezxc41abcdefghijklmnop
```

Or set it once for the session: `export CONNECT_SESSION=…`. If you have an
ordinary (non-SSO) Connect account you can use `-u you@example.edu` instead and
be prompted for a password.

**Connect sessions are short-lived.** If a download that worked yesterday says
"the server answered with the login page", the cookie has simply expired —
copy a fresh one.

---

## What you get

```
class07/
├── lecture.mp3        the voices, one continuous track, every mic levelled
├── lecture.mp4        shared screen + webcam picture-in-picture + audio
├── chat.txt           the chat transcript with timestamps
├── chapters.txt       the chapter/index markers
├── play.html          the offline player — this is the one you want
└── source/            the original streams, kept so you can rebuild
```

`play.html` needs no server and no network. The folder can be copied to a phone,
a USB stick or another machine and it still works.

---

## Watching a class back

Open `play.html` (or run `./connect-dl play ./class07`).

### Speed

Buttons from **0.5× to 16×**, with pitch correction, so a 2× lecture still
sounds like the lecturer rather than a chipmunk. <kbd>↑</kbd>/<kbd>↓</kbd> nudge
by 0.25 for the fine adjustment between them.

**Everything stays in sync at every speed.** The picture, the sound, the chat
highlight, the chapter highlight and the auto-scroll are all computed from the
media's own clock and from nothing else — there is no timer running alongside
that could drift — so the sidebar at 8× is exactly as correct as at 1×.

### Getting around

- A scrub bar with a **tick for every chapter**: hover to see its name, click to
  jump there.
- **Click any chat line or chapter** in the sidebar to jump to that moment.
- The current chapter is shown in the header as you go.
- Filter the chat with the search box (<kbd>/</kbd> focuses it) — useful when
  the answer you half-remember is somewhere in three hundred lines.
- Your **position and speed are remembered per recording**, so closing the tab
  and coming back tomorrow picks up where you were.

### Keyboard

| Key | Does |
|---|---|
| <kbd>space</kbd> / <kbd>K</kbd> | play / pause |
| <kbd>←</kbd> <kbd>→</kbd> | back / forward 10 seconds |
| <kbd>J</kbd> <kbd>L</kbd> | back / forward 30 seconds |
| <kbd>↑</kbd> <kbd>↓</kbd> | speed up / down by 0.25 |
| <kbd>0</kbd> | back to 1× |
| <kbd>Home</kbd> <kbd>End</kbd> | start / end |
| <kbd>/</kbd> | search the chat |

### Without the player

`lecture.mp3` is an ordinary audio file: VLC, mpv (`[` and `]` change speed
live) or any podcast app will play it at whatever speed you like. If you want a
permanently sped-up file for a car stereo with no speed control, add `--speed 2`
when you build it.

---

## Options

All optional.

| Option | Effect |
|---|---|
| `-o`, `--out DIR` | where to write the results (default: here) |
| `--session TOKEN` | the BREEZESESSION cookie (see above) |
| `-u`, `--user EMAIL` | sign in with a password instead (non-SSO accounts) |
| `--audio-only` | skip the video entirely — much faster |
| `--speed N` | also write a permanently sped-up audio copy, e.g. `2` |
| `--only-speaker N` | keep just one person's microphone (see `inspect`) |
| `--layout timeline\|sequential` | override how segments are placed in time |
| `--no-level` | leave the microphone levels exactly as recorded |
| `--render-annotations` | try to reconstruct whiteboard writing (see below) |
| `--no-open` | do not open the browser when the rebuild finishes |
| `--audio-format m4a\|opus\|wav` | default is `mp3` |
| `--no-camera` | leave the webcam out of the video |
| `--fps 5`, `--width`, `--height` | video frame rate and canvas (slides need very few frames) |
| `--offset NAME=MS` | move one stream by hand, e.g. `cameraVoip_1_2=2670000` |
| `--keep-source` | keep the downloaded zip after unpacking |
| `--dry-run` | print the ffmpeg commands instead of running them |
| `-v`, `-vv` | more detail (goes *before* the subcommand) |

### The other subcommands

```bash
./connect-dl list https://connect.example.edu/l993retztu2a/   # what recordings exist
./connect-dl inspect ./class07/source                         # timeline, speakers, mic levels
./connect-dl rebuild ./class07/source -o ./class07            # rebuild without downloading again
./connect-dl play ./class07                                   # reopen the player
```

`rebuild` is the one to reach for when something came out wrong: everything is
reconstructed from `source/`, so trying a different `--layout`, `--offset` or
`--only-speaker` costs seconds and no download.

---

## Whiteboard and handwritten annotations

**If your professor writes by hand on Connect's whiteboard, that writing is not
in the video.** Connect stores annotations as vector draw-commands in the
recording's event streams (`ftcontent*`, `indexstream*`), not as pixels in the
screen share. The Flash player redrew them live at playback time; the recorded
video simply does not contain them.

So a rebuilt lecture can have a blank board and still look like a finished
render. That is the failure this tool refuses to hand you silently.

**Every run reports what annotation data exists**, whether or not it can draw
it — how many events, in which streams, over what span, and which message names
they use:

```
annotations:
  ftcontent_1_1.flv    412 events over  38.2 min   [wb.drawStroke x331, onClearAll x6, ...]
  388 of those look like drawing (2914 points). Pass --render-annotations to try to draw them.
```

**To attempt reconstruction**, add `--render-annotations`:

```bash
./connect-dl get "<link>" -o ./class07 --render-annotations
```

You get an `annotations/` folder with one SVG per board state (split wherever
the board was cleared), a final-state SVG, and `annotations-report.txt`.

### What this can and cannot promise

The container work is solid: the event streams are decoded properly, message by
message, with real meeting timestamps. **The command vocabulary is not verified
against a real archive** — which name means "pen stroke" and in which order the
coordinates come is inferred. So:

- A message is only drawn if it looks unambiguously like drawing. Anything else
  is counted and written out, never guessed into a line that was not there.
- If nothing can be drawn, the tool says **plainly** that the whiteboard was not
  reproduced. It does not produce an empty SVG and call it done.
- `annotations-report.txt` is written **either way**, listing every message name
  with counts, the timestamp span, the AMF type bytes seen and the raw
  coordinate payloads. If your recording does not render, that file is exactly
  what is needed to add support for its dialect — it is worth keeping.

A reconstruction is a reconstruction: check it against the audio before trusting
it for anything that matters. If a lecture depended entirely on the whiteboard
and the report shows nothing drawable, the honest answer today is that you also
need the slides or someone's notes.

---

## Troubleshooting

**"the server answered with the Adobe Connect login page"**
The recording is not public and no valid session was sent. Either the cookie was
missing, or it has expired — Connect sessions are short-lived. Copy a fresh
`BREEZESESSION` (see above). Note that Connect answers an unauthenticated
request with HTTP **200** and a login page rather than an error, so this
message is the tool spotting a page pretending to be a download.

**Everybody talks at once / the lecture sounds like a jumble**
The recording's timestamps were start-up jitter rather than real positions and
were misread as positions. Rebuild with the segments laid end to end:

```bash
./connect-dl rebuild ./class07/source -o ./class07 --layout sequential
```

The opposite case — long silences, or the second half playing before the first —
wants `--layout timeline`. The tool prints which one it chose and why.

**A student's question is inaudible**
It should not be: microphones are levelled against each other by default,
because a lecturer's headset and a student on a laptop can differ by 30 dB, and
the tool reports which mics it raised. If you want the original levels, use
`--no-level`. If one person's mic is the *only* thing you want, `inspect` shows
the speaker numbers and `--only-speaker N` keeps just theirs.

**The download broke halfway**
Run the same command again. Large archives drop mid-transfer routinely; the
partial file is kept and the next run resumes from the bytes already on disk
with a Range request. The finished file is verified as a valid zip before it is
unpacked, so a truncated download is never silently rebuilt into a short lecture.

**No audio at all**
If the class used a **telephone bridge** rather than VoIP, that audio may not be
in the archive at all — only a Connect administrator can retrieve it. The tool
says so rather than producing a silent file.

**"is a meeting room, not a recording"**
Not fatal. Many servers serve the recording straight from the room handle, and
that is tried. If the room does list separate recordings, you are shown them and
asked which one.

**Certificate errors**
`--insecure` skips TLS verification, for servers behind an intercepting proxy.
Try without it first — most Connect servers verify normally.

---

## Two copies, on purpose

| | Where | Use it when |
|---|---|---|
| **maintained package** | `connect_dl/` + `./connect-dl` | normally — this is the one that gets fixed and tested |
| **zero-setup single file** | `standalone/ac_downloader.py` | copying one file is easier than copying a directory |

```bash
python3 standalone/ac_downloader.py "https://connect.example.edu/abc123/?session=TOKEN"
```

The standalone copy is a parallel implementation, not a build artifact. It
agrees with the package on every decision that changes the output — link
parsing, which files are metadata, when timestamps can be trusted, how mics are
levelled, what a login page looks like — and the test suite checks that it still
does. The package has more of everything: a from-scratch FLV parser that does
not need `ffprobe`, a fuller player, the tests.

---

## Tests

```bash
cd tools/connect-dl
python3 -m unittest discover tests        # 71 tests, a fraction of a second
```

Run them from `tools/connect-dl/` — `tests/` has no `__init__.py`, so pointing
discovery at it from the repository root fails with *Start directory is not
importable*. No ffmpeg and no network needed: the suite builds its own FLV files
and serves its own fake HTTP responses. GitHub Actions runs it on 3.9 and 3.13
for changes under `tools/connect-dl/`.

---

## How it works

1. **Fetch** — Connect builds a zip of a recording's source files on demand at
   `<link>/output/class.zip?download=zip`. The first request often returns a
   holding page while the job runs, so the download is a poll loop — and since
   an unauthenticated request also returns 200 with an HTML page, every response
   is checked for the zip magic before a byte is kept. Transfers resume from
   disk after a drop and the result is verified as a real zip.
2. **Identify** — each FLV is parsed directly (tag headers only, so it is fast
   even on gigabyte files) to find its codecs and the span of timestamps it
   covers, because Connect's own metadata is frequently missing or wrong. Files
   named `ftchat*`, `ftcontent*`, `indexstream*` and `transcriptstream*` are
   event streams, not media, and are never fed to ffmpeg.
3. **Align** — every stream is placed on one timeline: a start time stated in
   the XML wins, then the container's own timestamps, then laying segments end
   to end. The timestamps are only believed when their spread is large enough to
   be positions rather than start-up jitter — a quarter of the material, and at
   least 30 seconds. Getting this backwards does not cause a subtle drift, it
   reorders halves of the lecture, so it is decided once for the whole archive
   and the decision is printed.
4. **Level** — each microphone segment is measured on its own and raised toward
   a common loudness, capped so peaks cannot clip and so near-silence is not
   amplified into hiss. Quiet mics are only ever raised, never loud ones ducked.
5. **Render** — one ffmpeg pass per output. Audio segments are delayed onto
   their true positions and mixed, so a ten-minute break in the middle of a
   lecture stays a ten-minute silence rather than being closed up. Video is
   best-effort: a video failure is reported and never takes the audio with it.

---

You need to be entitled to the recording. This authenticates as you and fetches
what you already have access to — nothing more.

# connect-dl

Downloads an Adobe Connect class recording and rebuilds it into files you can
actually play — most importantly **one continuous audio track of the lecturer's
voice**, which you can then listen to at any speed.

Adobe Connect does not hand you a video file. A recording is stored as a pile of
separate Flash-era streams — the shared screen, the webcam, each stretch of
microphone audio — with no manifest a normal media player understands. This tool
fetches them, works out where each one belongs on the meeting's timeline, and
reassembles them.

## Requirements

- Python 3.9+ (standard library only — nothing to `pip install`)
- `ffmpeg`, for decoding Connect's Nellymoser/Speex audio and Flash Screen Video

```bash
sudo apt install ffmpeg      # Debian/Ubuntu
sudo dnf install ffmpeg      # Fedora
sudo pacman -S ffmpeg        # Arch
```

## Usage

```bash
# what recordings does this class room have?
./connect-dl list https://connect.example.edu/l993retztu2a/ -u me@example.edu

# download one and rebuild everything
./connect-dl get https://connect.example.edu/p8fj3k2la9x/ -u me@example.edu -o ./class07

# just the professor's voice, as quickly as possible
./connect-dl get https://connect.example.edu/p8fj3k2la9x/ --audio-only -o ./class07
```

Paste the link exactly as you have it — a recording link, a meeting-room link, or
the `connectpro://…` URL the desktop client is launched with. If the link points
at a room rather than a single recording, you are shown the list and asked which
one you want.

### Signing in

| Situation | What to use |
|---|---|
| Ordinary Connect account | `-u you@example.edu` (you are prompted for the password) |
| University SSO / Shibboleth | `--session BREEZESESSION_VALUE` |
| Recording is public | nothing |

For SSO accounts the XML login API is not usable at all. Log into Connect in a
browser, open developer tools → Application → Cookies, copy the value of the
`BREEZESESSION` cookie, and pass it with `--session`. You can also set
`CONNECT_SESSION` or `CONNECT_PASSWORD` in the environment instead of typing
them on the command line.

If the server sits behind an intercepting proxy with a self-signed certificate,
add `--insecure`.

## What you get

```
class07/
├── Signals-and-Systems-Session-7.mp3    the lecturer's voice, one continuous track
├── Signals-and-Systems-Session-7.mp4    shared screen + webcam + audio
├── play.html                            offline player with speed control
└── source/                              the original streams, kept for rebuilding
```

Open `play.html` in any browser. It reconstructs the parts of the Connect
playback experience worth having — the shared screen, a chapter list and the chat
transcript scrolling alongside — and adds the thing Connect's own player does
not offer: **any playback rate you like**, from 0.75× to 4×, with pitch
correction so a 2× lecture still sounds like the lecturer. Press <kbd>↑</kbd> and
<kbd>↓</kbd> to change speed, <kbd>space</kbd> to play, <kbd>J</kbd>/<kbd>L</kbd>
to skip 30 seconds. Your speed and position are remembered per recording.

If you would rather have a permanently sped-up file — for a phone or a car
stereo with no speed control — add `--speed 2`.

## Playback speed without any of the above

The `.mp3` is an ordinary audio file. VLC (`[` and `]`), mpv (`[` and `]`),
or any podcast app will play it at whatever speed you want.

## When the timing looks wrong

The hardest part of rebuilding a recording is working out when each stream
started, because Connect does not always say. The tool resolves this three ways,
and tells you which one it used for every stream:

| Source | Meaning |
|---|---|
| `xml` | a start time was stated in the recording's own metadata — reliable |
| `flv-timestamp` | taken from the stream's own timestamps — reliable |
| `sequential` / `assumed-zero` | **guessed** by laying segments end to end |

Anything marked as guessed is called out in the notes at the end of the run. If
part of the lecture drifts out of sync, correct it and rebuild — no re-download:

```bash
./connect-dl inspect ./class07/source                        # see the streams
./connect-dl rebuild ./class07/source -o ./class07 \
    --offset cameraVoip_1_2=2670000                          # in milliseconds
```

## Other options

| Option | Effect |
|---|---|
| `--audio-only` | skip video rendering entirely — much faster |
| `--audio-format m4a\|opus\|wav` | default is `mp3` |
| `--speed 2` | also write a permanently sped-up audio copy |
| `--fps 5` | video frame rate; slides need very few (default 5) |
| `--width/--height` | video canvas size (default 1280×720) |
| `--no-camera` | leave the webcam out of the video |
| `--no-player` | skip the HTML player |
| `--dry-run` | print the ffmpeg commands instead of running them |
| `-v` / `-vv` | more detail (goes before the subcommand) |

## How it works

1. **Fetch** — Connect builds a zip of a recording's source files on demand at
   `<recording-url>/output/…zip?download=zip`. The first request usually returns
   a holding page while the job runs, so the download is a poll loop. Newer
   HTML-client recordings sometimes offer a finished MP4 instead; that is taken
   as-is when available.
2. **Identify** — each FLV is parsed directly (tag headers only, so it is fast
   even on gigabyte files) to find its codecs and the span of timestamps it
   covers. Connect's metadata is frequently missing or wrong, so the container
   is trusted over what it claims.
3. **Align** — every stream is placed on one timeline. A single non-zero start
   anywhere in the recording tells us Connect stamped the streams with their
   real meeting positions; otherwise each segment restarts at zero and they are
   laid end to end. Getting this backwards does not cause a subtle drift — it
   reorders halves of the lecture — so it is decided once for the whole archive.
4. **Render** — one ffmpeg pass per output. Audio segments are delayed onto
   their true positions and mixed, so a ten-minute break in the middle of a
   lecture stays a ten-minute silence rather than being closed up.

## Limitations

- Audio recorded through a **telephone bridge** rather than VoIP may not be in
  the archive at all; only a Connect administrator can retrieve it.
- Whiteboard annotations, polls and slide *animations* are drawn by the Flash
  player at playback time from the event stream. The screen-share video is
  reproduced faithfully; those interactive overlays are not.
- If the account cannot download source files, the server returns a holding page
  forever. That is a permissions setting on the recording, not a bug here.

You need to be entitled to the recording — this authenticates as you and fetches
what you already have access to.

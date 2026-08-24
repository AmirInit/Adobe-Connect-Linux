"""Generation of the offline playback page.

The output is one self-contained HTML file next to the rendered media.  It
reconstructs the parts of the Connect playback experience that matter for
reviewing a lecture - the shared screen, the chapter index and the chat
transcript running alongside - and adds the thing Connect's own player does not
offer: an arbitrary playback rate.

The page references the media as sibling files rather than embedding it, so a
two-hour recording does not have to be base64'd into the document.  Everything
else (styles, script, transcript data) is inlined, so the folder can be copied
around and it still works with no server and no network.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .index import Events

__all__ = ["write_player", "SPEEDS"]

# The visible speed buttons.  These go far past what a normal player offers on
# purpose: skimming a two-hour class back at 8x or 16x to find the part you
# needed is the whole reason this page exists.  Fine adjustment in 0.25 steps
# is on the up/down arrows, and applyRate clamps anything to 0.25 - 16.
SPEEDS = [0.5, 0.75, 1, 1.25, 1.5, 1.75, 2, 2.5, 3, 4, 6, 8, 12, 16]


@dataclass
class PlayerSources:
    video: str | None = None
    audio: str | None = None
    poster: str | None = None


def write_player(
    out_path: Path,
    *,
    title: str,
    sources: PlayerSources,
    events: Events,
    duration_s: float,
    notes: list[str] | None = None,
) -> Path:
    payload = {
        "title": title,
        "video": sources.video,
        "audio": sources.audio,
        "duration": duration_s,
        "speeds": SPEEDS,
        "markers": [{"t": m.time_s, "label": m.label} for m in events.markers],
        "chat": [{"t": c.time_s, "from": c.sender, "msg": c.message} for c in events.chat],
        "notes": notes or [],
        "key": out_path.stem,
    }
    html = _TEMPLATE.replace("/*__DATA__*/null", json.dumps(payload, ensure_ascii=False))
    html = html.replace("__TITLE__", _escape(title))
    out_path.write_text(html, encoding="utf-8")
    return out_path


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;")
        .replace(">", "&gt;").replace('"', "&quot;")
    )


_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
  :root {
    --bg: #14161a; --panel: #1c1f26; --line: #2c313b;
    --fg: #e7e9ee; --muted: #99a0ae; --accent: #5b9dff; --accent-fg: #08101f;
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; margin: 0; }
  body {
    background: var(--bg); color: var(--fg);
    font: 15px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
    display: flex; flex-direction: column;
  }
  header {
    padding: 10px 16px; border-bottom: 1px solid var(--line);
    display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap;
  }
  header h1 { font-size: 16px; font-weight: 600; margin: 0; }
  header .meta { color: var(--muted); font-size: 13px; }
  main { flex: 1; display: flex; min-height: 0; }
  .stage {
    flex: 1; min-width: 0; display: flex; flex-direction: column;
    background: #000; position: relative;
  }
  .stage video, .stage audio { width: 100%; flex: 1; min-height: 0; background: #000; display: block; }
  .stage audio { height: 54px; flex: 0 0 auto; margin: auto; max-width: 640px; }
  .audio-only { flex: 1; display: grid; place-content: center; gap: 14px; text-align: center; padding: 32px; }
  .audio-only .big { font-size: 44px; }
  .audio-only p { color: var(--muted); margin: 0; max-width: 44ch; }

  .transport {
    background: var(--panel); border-top: 1px solid var(--line);
    padding: 10px 14px; display: flex; align-items: center; gap: 14px; flex-wrap: wrap;
  }
  .transport button {
    background: #262b35; color: var(--fg); border: 1px solid var(--line);
    border-radius: 6px; padding: 5px 10px; cursor: pointer; font: inherit; font-size: 13px;
  }
  .transport button:hover { border-color: var(--accent); }
  .speeds { display: flex; gap: 4px; flex-wrap: wrap; }
  .speeds button.on { background: var(--accent); color: var(--accent-fg); border-color: var(--accent); font-weight: 600; }
  .clock { color: var(--muted); font-variant-numeric: tabular-nums; font-size: 13px; }
  .rate-badge { font-weight: 700; color: var(--accent); font-variant-numeric: tabular-nums; }
  .rate-badge.fast { color: #ffc46b; }

  /* Scrub bar with a tick for every chapter, so the shape of the lecture is
     visible at a glance and a chapter is one click away. */
  .scrub {
    position: relative; height: 22px; cursor: pointer; background: var(--panel);
    border-top: 1px solid var(--line); flex: 0 0 auto;
  }
  .scrub .track {
    position: absolute; left: 0; right: 0; top: 9px; height: 4px;
    background: #333945; border-radius: 2px;
  }
  .scrub .played { position: absolute; left: 0; top: 9px; height: 4px; width: 0; background: var(--accent); border-radius: 2px; }
  .scrub .head {
    position: absolute; top: 5px; width: 12px; height: 12px; margin-left: -6px;
    border-radius: 50%; background: var(--fg); pointer-events: none;
  }
  .scrub .tick {
    position: absolute; top: 4px; width: 2px; height: 14px; margin-left: -1px;
    background: var(--muted); opacity: .8;
  }
  .scrub .tick:hover { background: var(--fg); }

  aside {
    width: 340px; flex: 0 0 340px; border-left: 1px solid var(--line);
    background: var(--panel); display: flex; flex-direction: column; min-height: 0;
  }
  .tabs { display: flex; border-bottom: 1px solid var(--line); }
  .tabs button {
    flex: 1; background: none; border: 0; border-bottom: 2px solid transparent;
    color: var(--muted); padding: 10px; cursor: pointer; font: inherit; font-size: 13px;
  }
  .tabs button.on { color: var(--fg); border-bottom-color: var(--accent); }
  .list { overflow-y: auto; flex: 1; padding: 6px; }
  .filter { padding: 6px 8px; border-bottom: 1px solid var(--line); }
  .filter input {
    width: 100%; background: #14161a; color: var(--fg); border: 1px solid var(--line);
    border-radius: 6px; padding: 5px 8px; font: inherit; font-size: 13px;
  }
  .filter input:focus { outline: none; border-color: var(--accent); }
  .row[hidden] { display: none; }
  .row {
    display: flex; gap: 10px; padding: 7px 8px; border-radius: 6px;
    cursor: pointer; align-items: baseline;
  }
  .row:hover { background: #262b35; }
  .row.now { background: #22314b; }
  .row .t { color: var(--accent); font-variant-numeric: tabular-nums; font-size: 12px; flex: 0 0 auto; }
  .row .who { color: var(--muted); font-size: 12px; }
  .row .txt { min-width: 0; overflow-wrap: anywhere; }
  .empty { color: var(--muted); padding: 18px; text-align: center; font-size: 13px; }
  .notes { padding: 10px 14px; border-top: 1px solid var(--line); color: var(--muted); font-size: 12px; }
  .notes li { margin: 3px 0; }
  kbd {
    background: #262b35; border: 1px solid var(--line); border-bottom-width: 2px;
    border-radius: 4px; padding: 0 5px; font-size: 11px; font-family: inherit;
  }
  @media (max-width: 820px) {
    main { flex-direction: column; }
    aside { width: auto; flex: 0 0 45%; border-left: 0; border-top: 1px solid var(--line); }
  }
</style>
</head>
<body>
<header>
  <h1 id="title"></h1>
  <span class="meta" id="meta"></span>
  <span class="meta" id="now-chapter"></span>
</header>

<main>
  <div class="stage">
    <div id="media"></div>
    <div class="scrub" id="scrub" title="click to seek">
      <div class="track"></div>
      <div class="played" id="played"></div>
      <div class="head" id="head"></div>
    </div>
    <div class="transport">
      <button id="play">Play</button>
      <button data-seek="-30">&#171; 30s</button>
      <button data-seek="-10">&#171; 10s</button>
      <button data-seek="10">10s &#187;</button>
      <button data-seek="30">30s &#187;</button>
      <span class="clock" id="clock">0:00 / 0:00</span>
      <span class="speeds" id="speeds"></span>
      <span class="rate-badge" id="rate">1x</span>
    </div>
  </div>

  <aside>
    <div class="tabs">
      <button data-tab="chapters" class="on">Chapters</button>
      <button data-tab="chat">Chat</button>
    </div>
    <div class="filter" id="filter-box" hidden>
      <input id="filter" type="search" placeholder="filter the chat&hellip;" autocomplete="off">
    </div>
    <div class="list" id="chapters"></div>
    <div class="list" id="chat" hidden></div>
    <div class="notes">
      <div><kbd>space</kbd> play &middot; <kbd>&larr;</kbd><kbd>&rarr;</kbd> 10s &middot;
           <kbd>J</kbd><kbd>L</kbd> 30s &middot; <kbd>&uarr;</kbd><kbd>&darr;</kbd> speed &middot;
           <kbd>0</kbd> 1x &middot; <kbd>Home</kbd>/<kbd>End</kbd> start/end</div>
      <ul id="notes"></ul>
    </div>
  </aside>
</main>

<script>
const DATA = /*__DATA__*/null;

const $ = (id) => document.getElementById(id);
const fmt = (s) => {
  if (!isFinite(s) || s < 0) s = 0;
  const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), x = Math.floor(s % 60);
  return (h ? h + ":" + String(m).padStart(2, "0") : String(m)) + ":" + String(x).padStart(2, "0");
};

document.title = DATA.title;
$("title").textContent = DATA.title;

// ---- media element -------------------------------------------------------
let media;
if (DATA.video) {
  media = document.createElement("video");
  media.src = DATA.video;
  media.controls = true;
  media.preload = "metadata";
  if (DATA.poster) media.poster = DATA.poster;
  $("media").replaceWith(media);
} else {
  const wrap = document.createElement("div");
  wrap.className = "audio-only";
  wrap.innerHTML = '<div class="big">&#127911;</div>' +
    '<p>This recording was reconstructed as audio only &mdash; the lecturer&rsquo;s voice ' +
    'on a single continuous timeline.</p>';
  media = document.createElement("audio");
  media.src = DATA.audio;
  media.controls = true;
  media.preload = "metadata";
  wrap.appendChild(media);
  $("media").replaceWith(wrap);
}

$("meta").textContent = (DATA.markers.length ? DATA.markers.length + " chapters · " : "") +
                        (DATA.chat.length ? DATA.chat.length + " chat lines · " : "") +
                        fmt(DATA.duration);

// ---- speed ---------------------------------------------------------------
// The whole point of the exercise: an arbitrary, persisted playback rate.
const STORE = "connect-dl:" + DATA.key;
const saved = JSON.parse(localStorage.getItem(STORE) || "{}");
let rate = saved.rate || 1;

function applyRate(r) {
  rate = Math.min(16, Math.max(0.25, Math.round(r * 100) / 100));
  // Everything on this page is driven from media.currentTime, so setting the
  // element's rate is the *only* thing speed has to change: the picture, the
  // sound, the chat highlight and the chapter follow all move together at the
  // new rate because they all read the same clock.
  media.playbackRate = rate;
  // Keep pitch correction on where the browser exposes it, so a 2x lecture
  // still sounds like the lecturer rather than a chipmunk.
  media.preservesPitch = true;
  media.mozPreservesPitch = true;
  media.webkitPreservesPitch = true;
  $("rate").textContent = rate + "x";
  $("rate").classList.toggle("fast", rate >= 4);
  for (const b of $("speeds").children) b.classList.toggle("on", Number(b.dataset.rate) === rate);
  persist();
}

const speedBox = $("speeds");
for (const s of DATA.speeds) {
  const b = document.createElement("button");
  b.textContent = s + "x";
  b.dataset.rate = s;
  b.onclick = () => applyRate(s);
  speedBox.appendChild(b);
}

// ---- transport -----------------------------------------------------------
const total = () => (isFinite(media.duration) && media.duration > 0) ? media.duration : DATA.duration;
const seekTo = (t) => { media.currentTime = Math.max(0, Math.min(total() - 0.25, t)); };
const seekBy = (d) => seekTo(media.currentTime + d);

$("play").onclick = () => media.paused ? media.play() : media.pause();
media.addEventListener("play", () => { $("play").textContent = "Pause"; follow(); });
media.addEventListener("pause", () => $("play").textContent = "Play");
for (const b of document.querySelectorAll("[data-seek]")) {
  b.onclick = () => seekBy(Number(b.dataset.seek));
}

// ---- scrub bar with a tick per chapter -----------------------------------
const scrub = $("scrub");
function layTicks() {
  for (const el of scrub.querySelectorAll(".tick")) el.remove();
  const dur = total();
  if (!dur) return;
  for (const m of DATA.markers) {
    const tick = document.createElement("div");
    tick.className = "tick";
    tick.style.left = (100 * Math.min(1, m.t / dur)) + "%";
    tick.title = fmt(m.t) + "  " + m.label;
    tick.onclick = (e) => { e.stopPropagation(); seekTo(m.t); };
    scrub.appendChild(tick);
  }
}
scrub.onclick = (e) => {
  const box = scrub.getBoundingClientRect();
  seekTo(((e.clientX - box.left) / box.width) * total());
};

let persistTimer = null;
function persist() {
  clearTimeout(persistTimer);
  persistTimer = setTimeout(() => {
    localStorage.setItem(STORE, JSON.stringify({ rate, time: media.currentTime }));
  }, 400);
}

media.addEventListener("loadedmetadata", () => {
  applyRate(rate);
  layTicks();
  // Pick up where this recording was left off, unless that was the very end.
  if (saved.time && saved.time < total() - 5) media.currentTime = saved.time;
  tick();
});

// ---- sidebar -------------------------------------------------------------
function fill(container, items, render) {
  if (!items.length) {
    container.innerHTML = '<div class="empty">Nothing recovered from this recording.</div>';
    return [];
  }
  const rows = items.map((item) => {
    const row = document.createElement("div");
    row.className = "row";
    row.innerHTML = render(item);
    // Clicking a line is the fastest way to get to the moment it belongs to.
    row.onclick = () => { seekTo(item.t); media.play(); };
    container.appendChild(row);
    return row;
  });
  return rows;
}

const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

const chapterRows = fill($("chapters"), DATA.markers,
  (m) => '<span class="t">' + fmt(m.t) + '</span><span class="txt">' + esc(m.label) + "</span>");
const chatRows = fill($("chat"), DATA.chat,
  (c) => '<span class="t">' + fmt(c.t) + '</span><span class="txt">' +
         (c.from ? '<span class="who">' + esc(c.from) + "</span> " : "") + esc(c.msg) + "</span>");

for (const b of document.querySelectorAll("[data-tab]")) {
  b.onclick = () => {
    for (const o of document.querySelectorAll("[data-tab]")) o.classList.toggle("on", o === b);
    const chat = b.dataset.tab === "chat";
    $("chapters").hidden = chat;
    $("chat").hidden = !chat;
    $("filter-box").hidden = !chat || !DATA.chat.length;
  };
}

// Filtering hides rows without disturbing the follow logic, which indexes the
// full list and simply skips over anything hidden when scrolling.
$("filter").oninput = () => {
  const needle = $("filter").value.trim().toLowerCase();
  chatRows.forEach((row, i) => {
    const c = DATA.chat[i];
    row.hidden = needle !== "" &&
      !((c.msg || "") + " " + (c.from || "")).toLowerCase().includes(needle);
  });
};

const notes = $("notes");
for (const n of DATA.notes) {
  const li = document.createElement("li");
  li.textContent = n;
  notes.appendChild(li);
}

// ---- follow playback -----------------------------------------------------
// Everything below reads media.currentTime and nothing else.  No wall-clock
// timer, no counter of its own: at 1x or at 16x the sidebar cannot drift away
// from the picture, because it is asking the picture what time it is.
function highlight(rows, items) {
  let active = -1;
  for (let i = 0; i < items.length; i++) if (items[i].t <= media.currentTime + 0.25) active = i;
  rows.forEach((row, i) => row.classList.toggle("now", i === active));
  return active;
}

let lastChat = -1, lastChapter = -1;
function tick() {
  const dur = total(), at = media.currentTime;
  $("clock").textContent = fmt(at) + " / " + fmt(dur);
  const pct = dur ? (100 * Math.min(1, at / dur)) : 0;
  $("played").style.width = pct + "%";
  $("head").style.left = pct + "%";

  const c = highlight(chapterRows, DATA.markers);
  if (c !== lastChapter) {
    lastChapter = c;
    $("now-chapter").textContent = c >= 0 ? "· " + DATA.markers[c].label : "";
    if (c >= 0 && !$("chapters").hidden) chapterRows[c].scrollIntoView({ block: "nearest" });
  }
  const i = highlight(chatRows, DATA.chat);
  if (i !== lastChat) {
    lastChat = i;
    if (i >= 0 && !$("chat").hidden && !chatRows[i].hidden) {
      chatRows[i].scrollIntoView({ block: "nearest" });
    }
  }
  persist();
}

// timeupdate alone fires about four times a second, which at 12x is nearly
// three seconds of lecture per update - visibly behind.  A frame loop while
// playing keeps it smooth; timeupdate still covers seeking and pauses.
let following = false;
function follow() {
  if (following) return;
  following = true;
  const step = () => {
    if (media.paused || media.ended) { following = false; tick(); return; }
    tick();
    requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}
media.addEventListener("timeupdate", () => { if (media.paused) tick(); });
media.addEventListener("seeked", tick);
media.addEventListener("durationchange", layTicks);

// ---- keyboard ------------------------------------------------------------
addEventListener("keydown", (e) => {
  if (e.target.tagName === "INPUT" || e.ctrlKey || e.metaKey || e.altKey) return;
  const step = 0.25;
  switch (e.key) {
    case " ": case "k": case "K": media.paused ? media.play() : media.pause(); break;
    case "ArrowLeft":  seekBy(-10); break;
    case "ArrowRight": seekBy(10); break;
    case "j": case "J": seekBy(-30); break;
    case "l": case "L": seekBy(30); break;
    case "ArrowUp":   applyRate(rate + step); break;
    case "ArrowDown": applyRate(rate - step); break;
    case "0": applyRate(1); break;
    case "Home": seekTo(0); break;
    case "End": seekTo(total() - 1); break;
    case "/":
      document.querySelector('[data-tab="chat"]').click();
      $("filter").focus();
      break;
    default: return;
  }
  e.preventDefault();
});
</script>
</body>
</html>
"""

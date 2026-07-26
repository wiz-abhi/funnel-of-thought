"""Caption + timing generator for the v2 demo video.

Source of truth: RECORDING-SCRIPT.md (8 sections, blockquoted narration).
`//` in the script is a *pause marker* — a strong cue-break hint, never
printed in a caption.

Output
------
  captions.ass   the burned-in captions: one line per cue, a translucent
                 padded strip behind the text (BorderStyle=3), and inline
                 colour on the key words. This is what assemble.py burns.
  captions.srt   plain-text accessibility side-cars for the YouTube upload.
  captions.vtt   Same words, same timings, no styling tags.

Two modes
---------
  python captions_v2.py --estimate
      No audio yet. Estimate each section's duration from its word count at
      145 wpm (2.41667 words/sec). Write audio-user/timings.json (file=null)
      and cut the caption files against those estimates.

  python captions_v2.py --from-audio
      The human's recordings have landed in audio-user/sectionN.{wav,m4a,mp3}.
      For each: convert to wav, trim leading/trailing silence (silenceremove,
      ~-38dB, ~200ms pad) into audio-user/trimmed/sectionN.wav, measure the
      real duration, concat into audio-user/narration.wav (no gaps), write
      audio-user/timings.json, and regenerate all caption files against the
      *measured* durations. Prints a per-section table with the <3:00 guard.

  python captions_v2.py --verify-render
      Renders every cue in captions.ass through libass and measures the real
      pixel bounding box of each strip. This is how MAX_CUE_CHARS was chosen;
      re-run it if the font, size or margins change.

Guard: the finished video (narration + 3.5s end card) must stay < 3:00.
Warn above 2:55, hard-fail above 2:59.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

# ---- keep UTF-8 sane on Windows / PowerShell 5.1 ---------------------------
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = Path(__file__).parent
SCRIPT = HERE / "RECORDING-SCRIPT.md"
USER = HERE / "audio-user"
TRIMMED = USER / "trimmed"
TIMINGS = USER / "timings.json"
NARRATION = USER / "narration.wav"
SRT = HERE / "captions.srt"
VTT = HERE / "captions.vtt"
ASS = HERE / "captions.ass"

WPM = 145.0
WPS = WPM / 60.0                # 2.41667 words / second
TAIL = 3.5                      # end-card still appended after §8

# --------------------------------------------------------------------------- #
# caption shape
# --------------------------------------------------------------------------- #
# ONE line per cue, always. A cue longer than the budget is split into more
# cues rather than allowed to wrap (WrapStyle 2 in the ASS would otherwise
# just run off the frame).
#
# The budget is measured, not guessed: --verify-render rasterises every cue
# through libass and reports the strip's real bounding box. At 44px Segoe UI
# Semibold, 62 chars of this script's prose renders a ~990px strip; the widest
# plausible 62-char string (all caps/wide glyphs) measures ~1334px. The usable
# width is PlayResX - MarginL - MarginR = 1920 - 80 - 80 = 1760px, so 62 chars
# clears the frame with room to spare even in the worst case.
MAX_CUE_CHARS = 62              # visible chars per cue (single line)
STRIP_MAX_PX = 1920 - 2 * 80    # box must fit inside the L/R margins
MIN_CUE = 1.2                   # seconds
MAX_CUE = 5.0                   # seconds

# --------------------------------------------------------------------------- #
# ASS style
# --------------------------------------------------------------------------- #
# ASS colours are &HAABBGGRR — AA is *transparency* (00 opaque, FF invisible)
# and the RGB bytes are reversed. So #F2F5F8 text -> &H00F8F5F2, and the strip
# is black at 0x66/255 = 40% transparent -> &H66000000.
ASS_FONT = "Segoe UI Semibold"  # verified present (seguisb.ttf); libass
                                # fontselect resolves it to SegoeUI-Semibold
ASS_SIZE = 44
ASS_MARGIN_V = 56               # strip sits in the clear band at frame bottom
ASS_MARGIN_LR = 80
ASS_OUTLINE = 14                # BorderStyle=3 -> this is the box's padding

C_TEXT = "&H00F8F5F2"           # #F2F5F8 near-white
C_HIDDEN = "&HFF000000"         # fully transparent fill (strip layer)
C_STRIP = "&H66000000"          # black, 40% transparent
C_GREEN = "&H0094C24C"          # #4CC294
C_AMBER = "&H004D7AFF"          # #FF7A4D


def _tag(colour: str) -> str:
    r"""&HAABBGGRR (style form) -> {\c&HBBGGRR&} (inline override form)."""
    return "{\\c&H" + colour[4:] + "&}"


T_TEXT, T_GREEN, T_AMBER = _tag(C_TEXT), _tag(C_GREEN), _tag(C_AMBER)

# --------------------------------------------------------------------------- #
# emphasis rules — restrained: at most MAX_SPANS coloured spans per cue.
#   green = the funnel's own evidence and the things I shipped
#   amber = the gap, the number that lies, and the punchline words
# The narration spells numbers out, so the patterns match words first and
# digits second (the digit forms are there for on-screen paraphrases).
# --------------------------------------------------------------------------- #
MAX_SPANS = 2

EMPHASIS: list[tuple[str, str]] = [
    # --- green: product / technical names -------------------------------- #
    (r"\bFunnel of Thought\b", T_GREEN),
    (r"\bsignoz-funnel-mcp\b", T_GREEN),
    (r"\bOpenTelemetry(?:-instrumented)?\b", T_GREEN),
    (r"\bTrace Funnels\b", T_GREEN),
    (r"\bSigNoz(?:'s)?\b", T_GREEN),
    (r"\bfot\b", T_GREEN),
    # --- green: the funnel's numbers (the honest ones) -------------------- #
    (r"\bone hundred and twenty-five\b", T_GREEN),
    (r"\bOne twenty-five\b", T_GREEN),
    (r"\bsixty-four percent\b", T_GREEN),
    (r"\b125\b", T_GREEN),
    (r"\b64%", T_GREEN),
    (r"\beighty\b", T_GREEN),
    (r"\b80\b", T_GREEN),
    # --- amber: the gap, the lie, the punchline --------------------------- #
    (r"\bone hundred percent\b", T_AMBER),
    (r"\b100%", T_AMBER),
    (r"\bthirty-six percent\b", T_AMBER),
    (r"\b36-point\b", T_AMBER),
    (r"\b36%", T_AMBER),
    (r"\bForty-five\b", T_AMBER),
    (r"\b45\b", T_AMBER),
    (r"\bpresence\b", T_AMBER),
    (r"\bsequence\b", T_AMBER),
    (r"\bbefore\b", T_AMBER),
    (r"\bafter\b", T_AMBER),
    # last: the bare "one hundred" of §5 ("the counter say one hundred?").
    # Everything richer than it matched above, and matched spans are never
    # re-covered, so this only ever catches the leftover.
    (r"\bone hundred\b", T_AMBER),
]
EMPHASIS_RE = [(re.compile(p, re.IGNORECASE), c) for p, c in EMPHASIS]

# Phrases that must never be split across two cues — a number cut in half
# ("the same one" / "hundred and twenty-five traces") reads terribly and loses
# its colour. These are held together during line splitting.
ATOMIC = [re.compile(p, re.IGNORECASE) for p in (
    r"one hundred and twenty-five",
    r"one twenty-five",
    r"one hundred percent",
    r"sixty-four percent",
    r"thirty-six percent",
    r"Funnel of Thought",
    r"Trace Funnels",
)]
NBSP = "\x00"                       # internal marker, never reaches a file

# <3:00 guard (measured against narration + TAIL)
WARN_S = 2 * 60 + 55            # 175 -> warn
FAIL_S = 2 * 60 + 59            # 179 -> hard fail

# silence trim
SILENCE_DB = "-38dB"
SILENCE_PAD = 0.2              # keep ~200ms head/tail


# --------------------------------------------------------------------------- #
# script parsing
# --------------------------------------------------------------------------- #
def clean_inline(s: str) -> str:
    """Strip markdown decoration; keep the words verbatim. `//` removed."""
    s = s.replace("//", " ")
    s = s.replace("**", "").replace("`", "")
    # braces are ASS override-tag syntax; the script has none, keep it that way
    s = s.replace("{", "(").replace("}", ")")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def parse_script(path: Path) -> list[dict]:
    """Return [{n, target, raw (with //), words}] for each ## §N section."""
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    sections: list[dict] = []
    cur: dict | None = None
    for ln in lines:
        m = re.match(r"^##\s*§\s*(\d+)\b", ln)
        if m:
            if cur:
                sections.append(cur)
            target = None
            tm = re.search(r"~\s*(\d+(?:\.\d+)?)\s*s\b", ln)
            if tm:
                target = float(tm.group(1))
            cur = {"n": int(m.group(1)), "target": target, "quotes": []}
            continue
        if cur is not None and ln.lstrip().startswith(">"):
            cur["quotes"].append(ln.lstrip()[1:].strip())
    if cur:
        sections.append(cur)

    for s in sections:
        raw = " ".join(q for q in s["quotes"])          # keep // for splitting
        s["raw"] = raw
        s["words"] = len(clean_inline(raw).split())
        del s["quotes"]
    sections.sort(key=lambda s: s["n"])
    return sections


# --------------------------------------------------------------------------- #
# cue splitting / wrapping
# --------------------------------------------------------------------------- #
def _greedy(words: list[str], width: int) -> list[str]:
    """Greedy word wrap at `width` chars. Over-long tokens get their own line."""
    lines: list[str] = []
    cur = ""
    for w in words:
        while len(w) > width:                       # never happens in this script
            if cur:
                lines.append(cur)
                cur = ""
            lines.append(w[:width])
            w = w[width:]
        if not cur:
            cur = w
        elif len(cur) + 1 + len(w) <= width:
            cur += " " + w
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def split_single_lines(phrase: str, budget: int = MAX_CUE_CHARS) -> list[str]:
    """Split a phrase into the fewest single-line cues of <= budget chars.

    Balanced, not greedy: once the cue count k is known, the narrowest width
    that still yields k lines is used, so we get two 46-char cues rather than
    a 62-char cue followed by a 30-char runt.
    """
    for rx in ATOMIC:                               # hold key phrases together
        phrase = rx.sub(lambda m: m.group(0).replace(" ", NBSP), phrase)
    words = phrase.split()
    if not words:
        return []
    k = len(_greedy(words, budget))
    lo = max(len(w) for w in words)
    hi = budget
    best = _greedy(words, hi)
    while lo <= hi:                                 # binary search the width
        mid = (lo + hi) // 2
        trial = _greedy(words, mid)
        if len(trial) <= k:
            best = trial
            hi = mid - 1
        else:
            lo = mid + 1
    return [ln.replace(NBSP, " ") for ln in best]


def build_cues(raw: str) -> list[str]:
    """Split a section's raw narration into single-line cue strings.

    Hard breaks at `//` (pauses): words from different phrases never share a
    cue, so every pause starts a fresh caption. Every cue is ONE line of at
    most MAX_CUE_CHARS visible characters — longer prose becomes more cues,
    never a second stacked line.
    """
    phrases = [clean_inline(p) for p in raw.split("//")]
    cues: list[str] = []
    for ph in phrases:
        if ph:
            cues.extend(split_single_lines(ph))
    return cues


# --------------------------------------------------------------------------- #
# emphasis
# --------------------------------------------------------------------------- #
def emphasize(text: str) -> str:
    """Wrap up to MAX_SPANS key phrases in inline ASS colour overrides."""
    spans: list[tuple[int, int, str]] = []
    for rx, colour in EMPHASIS_RE:
        for m in rx.finditer(text):
            if m.start() == m.end():
                continue
            if any(m.start() < e and s < m.end() for s, e, _ in spans):
                continue                            # already covered
            spans.append((m.start(), m.end(), colour))
    spans.sort()
    spans = spans[:MAX_SPANS]
    out = []
    pos = 0
    for s, e, colour in spans:
        out.append(text[pos:s])
        out.append(f"{colour}{text[s:e]}{T_TEXT}")
        pos = e
    out.append(text[pos:])
    return "".join(out)


TAG_RE = re.compile(r"\{[^}]*\}")


def strip_tags(s: str) -> str:
    return TAG_RE.sub("", s)


# --------------------------------------------------------------------------- #
# duration apportioning
# --------------------------------------------------------------------------- #
def apportion(weights: list[float], total: float,
              lo: float = MIN_CUE, hi: float = MAX_CUE) -> list[float]:
    """Split `total` across cues proportional to `weights`, clamped to [lo,hi].

    Guarantees sum == total (so captions never drift from the audio) and
    monotonic layout when laid end to end. If the cue count makes [lo,hi]
    infeasible, falls back to an equal split.
    """
    n = len(weights)
    if n == 0:
        return []
    if n * lo > total + 1e-6:
        return [total / n] * n
    wsum = sum(weights) or 1.0
    d = [total * w / wsum for w in weights]
    for _ in range(60):
        d = [min(hi, max(lo, x)) for x in d]
        diff = total - sum(d)
        if abs(diff) < 1e-6:
            break
        if diff > 0:
            adj = [i for i, x in enumerate(d) if x < hi - 1e-9]
        else:
            adj = [i for i, x in enumerate(d) if x > lo + 1e-9]
        if not adj:
            break
        share = diff / len(adj)
        for i in adj:
            d[i] += share
    resid = total - sum(d)
    if abs(resid) > 1e-6:                      # keep the sum exact
        d[-1] += resid
    return d


# --------------------------------------------------------------------------- #
# caption file writers
# --------------------------------------------------------------------------- #
def _ts(t: float, sep: str) -> str:
    if t < 0:
        t = 0.0
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def build_cue_timeline(sections: list[dict], durations: list[float]) -> list[dict]:
    """One flat, monotonic list of cues across all sections."""
    cues: list[dict] = []
    t = 0.0
    for sec, dur in zip(sections, durations):
        texts = build_cues(sec["raw"])
        weights = [max(len(x), 1) for x in texts]
        parts = apportion(weights, dur)
        for text, plen in zip(texts, parts):
            start = t
            end = t + plen
            cues.append({"start": start, "end": end, "text": text})
            t = end
    return cues


def write_srt(cues: list[dict], path: Path) -> None:
    out = []
    for i, c in enumerate(cues, 1):
        out.append(str(i))
        out.append(f"{_ts(c['start'], ',')} --> {_ts(c['end'], ',')}")
        out.append(c["text"])
        out.append("")
    path.write_text("\n".join(out), encoding="utf-8")


def write_vtt(cues: list[dict], path: Path) -> None:
    out = ["WEBVTT", ""]
    for i, c in enumerate(cues, 1):
        out.append(str(i))
        out.append(f"{_ts(c['start'], '.')} --> {_ts(c['end'], '.')}")
        out.append(c["text"])
        out.append("")
    path.write_text("\n".join(out), encoding="utf-8")


# --------------------------------------------------------------------------- #
# ASS (the burned-in captions)
# --------------------------------------------------------------------------- #
STYLE_FIELDS = ("Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
                "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
                "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
                "Alignment, MarginL, MarginR, MarginV, Encoding")

# Two styles, two Dialogue lines per cue, and this is not decoration:
#
#   Strip  BorderStyle=3 + Outline=14 + Shadow=0 draws an opaque-box border in
#          OutlineColour — a translucent strip padded 14px around the glyphs.
#          Its own glyphs are invisible (PrimaryColour alpha FF).
#   Main   the same text, same font/size/margins so it lands in exactly the
#          same place, drawn on layer 1 with BorderStyle=1/Outline=0 and the
#          inline colour overrides.
#
# Why not one line with the box and the colours together: libass groups the
# border shape per colour run, so each {\c} span gets its OWN padded box. The
# 14px pads overlap at every span boundary and the 40%-transparent black
# compounds there into dark notches. A single uncoloured strip line has one
# continuous run and therefore one flat, even band.
STYLE_LINES = "\n".join((
    f"Style: Strip,{ASS_FONT},{ASS_SIZE},{C_HIDDEN},{C_HIDDEN},{C_STRIP},"
    f"&H00000000,0,0,0,0,100,100,0,0,3,{ASS_OUTLINE},0,2,"
    f"{ASS_MARGIN_LR},{ASS_MARGIN_LR},{ASS_MARGIN_V},1",
    f"Style: Main,{ASS_FONT},{ASS_SIZE},{C_TEXT},{C_TEXT},{C_STRIP},"
    f"&H00000000,0,0,0,0,100,100,0,0,1,0,0,2,"
    f"{ASS_MARGIN_LR},{ASS_MARGIN_LR},{ASS_MARGIN_V},1",
))

ASS_HEADER = f"""[Script Info]
; Generated by captions_v2.py — do not hand-edit.
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
WrapStyle: 2
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: {STYLE_FIELDS}
{STYLE_LINES}

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def _ass_ts(t: float) -> str:
    """ASS timestamps are H:MM:SS.cc (centiseconds)."""
    if t < 0:
        t = 0.0
    cs = int(round(t * 100))
    h, cs = divmod(cs, 360_000)
    m, cs = divmod(cs, 6_000)
    s, cs = divmod(cs, 100)
    return f"{h:d}:{m:02d}:{s:02d}.{cs:02d}"


def write_ass(cues: list[dict], path: Path) -> None:
    lines = [ASS_HEADER]
    for c in cues:
        a, b = _ass_ts(c["start"]), _ass_ts(c["end"])
        lines.append(f"Dialogue: 0,{a},{b},Strip,,0,0,0,,{c['text']}")
        lines.append(f"Dialogue: 1,{a},{b},Main,,0,0,0,,{emphasize(c['text'])}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def read_dialogues(path: Path) -> list[tuple[str, float, float, str]]:
    """[(style, start, end, text)] for every Dialogue line in an ASS file."""
    out = []
    for ln in path.read_text(encoding="utf-8").splitlines():
        if not ln.startswith("Dialogue:"):
            continue
        f = ln.split(",", 9)
        out.append((f[3], _parse_ass_ts(f[1]), _parse_ass_ts(f[2]), f[9]))
    return out


def assert_ass(path: Path) -> int:
    """Fail loudly if the generated ASS could ever render as two lines.

    Checks, over the file as written: Strip/Main lines pair up and carry the
    same words; no line breaks (`\\N`, `\\n`, `\\h`, literal newlines) in any
    Dialogue; every cue's *visible* text within the character budget; timings
    positive, monotonic and non-overlapping.
    """
    rows = read_dialogues(path)
    if not rows or len(rows) % 2:
        sys.exit(f"ASS ASSERT FAILED: {path.name} has {len(rows)} Dialogue "
                 f"lines — expected two (Strip + Main) per cue")
    prev_end = -1.0
    for i in range(0, len(rows), 2):
        (s_style, s_st, s_en, s_txt) = rows[i]
        (m_style, m_st, m_en, m_txt) = rows[i + 1]
        n = i // 2 + 1
        if (s_style, m_style) != ("Strip", "Main"):
            sys.exit(f"ASS ASSERT FAILED: cue {n} styles are "
                     f"{s_style}/{m_style}, expected Strip/Main")
        if (s_st, s_en) != (m_st, m_en):
            sys.exit(f"ASS ASSERT FAILED: cue {n} strip/text timings differ")
        if strip_tags(m_txt) != s_txt:
            sys.exit(f"ASS ASSERT FAILED: cue {n} strip and text disagree:\n"
                     f"  strip: {s_txt!r}\n  text : {strip_tags(m_txt)!r}")
        for body in (s_txt, m_txt):
            if "\\N" in body or "\\n" in body or "\\h" in body:
                sys.exit(f"ASS ASSERT FAILED: cue {n} contains a line break: "
                         f"{body!r}")
        if len(s_txt) > MAX_CUE_CHARS:
            sys.exit(f"ASS ASSERT FAILED: cue {n} is {len(s_txt)} chars "
                     f"> budget {MAX_CUE_CHARS}: {s_txt!r}")
        if m_en <= m_st:
            sys.exit(f"ASS ASSERT FAILED: cue {n} ends at or before it starts")
        if m_st < prev_end - 1e-6:
            sys.exit(f"ASS ASSERT FAILED: cue {n} overlaps the previous cue "
                     f"({m_st:.2f} < {prev_end:.2f})")
        prev_end = m_en
    return len(rows) // 2


def _parse_ass_ts(s: str) -> float:
    h, m, rest = s.split(":")
    return int(h) * 3600 + int(m) * 60 + float(rest)


def write_timings(sections: list[dict], durations: list[float],
                  files: list[str | None]) -> float:
    entries = []
    for sec, dur, f in zip(sections, durations, files):
        target = sec["target"]
        entries.append({
            "n": sec["n"],
            "file": f,
            "duration": round(dur, 3),
            "target": target,
            "delta": round(dur - target, 3) if target is not None else None,
        })
    total = round(sum(durations), 3)
    USER.mkdir(exist_ok=True)
    TIMINGS.write_text(
        json.dumps({"sections": entries, "total_duration": total}, indent=1),
        encoding="utf-8",
    )
    return total


# --------------------------------------------------------------------------- #
# guard / reporting
# --------------------------------------------------------------------------- #
def fmt_mmss(t: float) -> str:
    return f"{int(t) // 60}:{int(round(t)) % 60:02d}"


def report_and_guard(sections: list[dict], durations: list[float],
                     ncues: int, mode: str) -> None:
    print(f"\n  mode: {mode}   (145 wpm estimate)" if mode == "estimate"
          else f"\n  mode: {mode}   (measured from recordings)")
    print(f"  {'sec':>3}  {'words':>5}  {'target':>7}  {'dur':>7}  {'delta':>7}")
    for sec, dur in zip(sections, durations):
        tgt = sec["target"]
        tgt_s = f"{tgt:5.1f}s" if tgt is not None else "   -  "
        dlt = f"{dur - tgt:+5.1f}s" if tgt is not None else "   -  "
        print(f"  §{sec['n']:>2}  {sec['words']:>5}  {tgt_s:>7}  "
              f"{dur:5.1f}s  {dlt:>7}")

    narration = sum(durations)
    video_total = narration + TAIL
    print(f"\n  narration total : {narration:6.1f}s  ({fmt_mmss(narration)})")
    print(f"  + end card tail : {TAIL:6.1f}s")
    print(f"  VIDEO TOTAL     : {video_total:6.1f}s  ({fmt_mmss(video_total)})")
    print(f"  caption cues    : {ncues}")

    if video_total > FAIL_S:
        sys.exit(f"\n  FAIL: video is {fmt_mmss(video_total)} > 2:59 "
                 f"(hard cap 3:00). Re-record the slowest section.")
    if video_total > WARN_S:
        print(f"\n  WARN: video is {fmt_mmss(video_total)} > 2:55 — "
              f"trim margin is thin; consider a tighter read.")
    else:
        print(f"\n  OK: {fmt_mmss(video_total)} < 2:55, comfortable under 3:00.")


# --------------------------------------------------------------------------- #
# audio helpers (--from-audio)
# --------------------------------------------------------------------------- #
def run(cmd: list[str], cwd: Path | None = None) -> None:
    p = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)
    if p.returncode != 0:
        sys.exit(f"FAILED: {' '.join(cmd[:6])} …\n{p.stderr[-1500:]}")


def probe_duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    )
    return float(out.stdout.strip())


def find_section_audio(n: int) -> Path | None:
    for ext in (".wav", ".m4a", ".mp3", ".WAV", ".M4A", ".MP3"):
        p = USER / f"section{n}{ext}"
        if p.exists():
            return p
    return None


def trim_silence(src: Path, dst: Path) -> None:
    """Convert to mono 48k wav and trim leading/trailing silence (kept pad)."""
    sr = (f"silenceremove=start_periods=1:start_silence={SILENCE_PAD}:"
          f"start_threshold={SILENCE_DB}")
    af = f"{sr},areverse,{sr},areverse"
    run(["ffmpeg", "-y", "-i", str(src), "-af", af,
         "-ar", "48000", "-ac", "1", "-c:a", "pcm_s16le", str(dst)])


def concat_wavs(parts: list[Path], dst: Path) -> None:
    listfile = TRIMMED / "concat.txt"
    listfile.write_text("\n".join(f"file '{p.as_posix()}'" for p in parts),
                        encoding="utf-8")
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(listfile),
         "-ac", "1", "-ar", "48000", "-c:a", "pcm_s16le", str(dst)])


# --------------------------------------------------------------------------- #
# modes
# --------------------------------------------------------------------------- #
def write_captions(cues: list[dict]) -> None:
    """ASS for burn-in, SRT/VTT as plain-text side-cars, then assert."""
    write_ass(cues, ASS)
    write_srt(cues, SRT)
    write_vtt(cues, VTT)
    n = assert_ass(ASS)
    for p in (SRT, VTT):
        body = p.read_text(encoding="utf-8")
        if "{\\" in body or "\\c&H" in body:
            sys.exit(f"ASSERT FAILED: {p.name} contains ASS override tags")
    print(f"wrote {ASS}   ({n} cues, 1 line each, <= {MAX_CUE_CHARS} chars)")
    print(f"wrote {SRT}  /  {VTT}   (plain text side-cars)")


def do_estimate(sections: list[dict]) -> None:
    durations = [s["words"] / WPS for s in sections]
    files = [None] * len(sections)
    write_timings(sections, durations, files)
    cues = build_cue_timeline(sections, durations)
    print(f"wrote {TIMINGS}")
    write_captions(cues)
    report_and_guard(sections, durations, len(cues), "estimate")


def do_from_audio(sections: list[dict]) -> None:
    if not USER.exists():
        sys.exit(f"no {USER} directory — drop section1..8 recordings there first")
    TRIMMED.mkdir(parents=True, exist_ok=True)
    durations: list[float] = []
    files: list[str] = []
    trimmed_parts: list[Path] = []
    missing = []
    for s in sections:
        src = find_section_audio(s["n"])
        if src is None:
            missing.append(f"audio-user/section{s['n']}.(wav|m4a|mp3)")
            continue
        dst = TRIMMED / f"section{s['n']}.wav"
        trim_silence(src, dst)
        dur = probe_duration(dst)
        durations.append(dur)
        files.append(f"audio-user/trimmed/section{s['n']}.wav")
        trimmed_parts.append(dst)
    if missing:
        sys.exit("MISSING recordings:\n  - " + "\n  - ".join(missing))

    concat_wavs(trimmed_parts, NARRATION)
    write_timings(sections, durations, files)
    cues = build_cue_timeline(sections, durations)
    print(f"wrote {NARRATION}  ({probe_duration(NARRATION):.1f}s)")
    print(f"wrote {TIMINGS}")
    write_captions(cues)
    report_and_guard(sections, durations, len(cues), "from-audio")


# --------------------------------------------------------------------------- #
# --verify-render: rasterise the real cues and measure the real strip
# --------------------------------------------------------------------------- #
def do_verify_render() -> None:
    """Render every cue through libass and measure its strip bounding box.

    Each cue is re-timed to one second on a 1920x1080 white field, dumped as
    raw gray8, and the bounding box of everything that is not background is
    computed in pure Python (no imaging deps). This is the empirical check
    behind MAX_CUE_CHARS: it proves the widest cue's strip fits the frame and
    that every cue is a single line of text.
    """
    if not ASS.exists():
        sys.exit(f"no {ASS.name} — run --estimate or --from-audio first")
    src = ASS.read_text(encoding="utf-8")
    head, _, events = src.partition("[Events]")
    fmt = [ln for ln in events.splitlines() if ln.startswith("Format:")][0]
    rows = read_dialogues(ASS)
    cues = [(rows[i][3], rows[i + 1][3]) for i in range(0, len(rows), 2)]

    probe = HERE / "_verify.ass"
    out = [head, "[Events]", fmt]
    for i, (strip, text) in enumerate(cues):       # one second per cue
        a, b = _ass_ts(i), _ass_ts(i + 1)
        out.append(f"Dialogue: 0,{a},{b},Strip,,0,0,0,,{strip}")
        out.append(f"Dialogue: 1,{a},{b},Main,,0,0,0,,{text}")
    probe.write_text("\n".join(out) + "\n", encoding="utf-8")

    W, H = 1920, 1080
    p = subprocess.run(
        ["ffmpeg", "-v", "info", "-y", "-f", "lavfi",
         "-i", f"color=white:s={W}x{H}:r=1:d={len(cues)}",
         "-vf", f"ass={probe.name},format=gray",
         "-f", "rawvideo", "-pix_fmt", "gray", "-"],
        capture_output=True, cwd=HERE)
    err = p.stderr.decode("utf-8", "replace")
    for ln in err.splitlines():
        if "fontselect" in ln:
            print("  font:", ln.split("] ", 1)[-1])
    data = p.stdout
    if len(data) < W * H * len(cues):
        sys.exit(f"render failed:\n{err[-1500:]}")

    frame_h = 0
    widest = (0, "")
    bad = []
    for i, (visible, _text) in enumerate(cues):
        buf = data[i * W * H:(i + 1) * W * H]
        bg = buf[0]
        x0, x1, y0, y1 = W, -1, H, -1
        for y in range(H):
            row = buf[y * W:(y + 1) * W]
            if row.count(bg) == W:
                continue
            y0 = min(y0, y)
            y1 = y
            lo = 0
            while lo < W and row[lo] == bg:
                lo += 1
            hi = W - 1
            while hi >= 0 and row[hi] == bg:
                hi -= 1
            x0, x1 = min(x0, lo), max(x1, hi)
        w, h = x1 - x0 + 1, y1 - y0 + 1
        frame_h = max(frame_h, h)
        if w > widest[0]:
            widest = (w, visible)
        if x0 < 8 or x1 > W - 9:
            bad.append(f"cue {i+1} runs off frame (x {x0}..{x1}): {visible!r}")
        if h > ASS_SIZE * 2:                 # a 2nd line would ~double the box
            bad.append(f"cue {i+1} rendered {h}px tall — WRAPPED: {visible!r}")
    probe.unlink(missing_ok=True)

    print(f"  cues measured   : {len(cues)}")
    print(f"  usable width    : {STRIP_MAX_PX}px "
          f"(1920 - 2x{ASS_MARGIN_LR} margin)")
    print(f"  widest strip    : {widest[0]}px  {widest[1]!r}")
    print(f"  strip height    : {frame_h}px (single line at {ASS_SIZE}px + "
          f"2x{ASS_OUTLINE}px padding)")
    if bad:
        print("\n".join("  FAIL: " + b for b in bad))
        sys.exit("render verification FAILED")
    print(f"  OK: every strip is one line and fits inside the frame "
          f"({widest[0]} <= {STRIP_MAX_PX}).")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--estimate", action="store_true",
                   help="no audio yet: estimate durations at 145 wpm")
    g.add_argument("--from-audio", action="store_true",
                   help="measure real durations from audio-user/sectionN.*")
    g.add_argument("--verify-render", action="store_true",
                   help="rasterise captions.ass and measure every strip")
    args = ap.parse_args()

    if args.verify_render:
        do_verify_render()
        return

    sections = parse_script(SCRIPT)
    if len(sections) != 8:
        sys.exit(f"expected 8 sections in {SCRIPT.name}, found {len(sections)}")

    if args.estimate:
        do_estimate(sections)
    else:
        do_from_audio(sections)


if __name__ == "__main__":
    main()

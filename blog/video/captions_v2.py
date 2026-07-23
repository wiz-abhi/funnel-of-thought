"""Caption + timing generator for the v2 demo video.

Source of truth: RECORDING-SCRIPT.md (8 sections, blockquoted narration).
`//` in the script is a *pause marker* — a strong cue-break hint, never
printed in a caption.

Two modes
---------
  python captions_v2.py --estimate
      No audio yet. Estimate each section's duration from its word count at
      145 wpm (2.41667 words/sec). Write audio-user/timings.json (file=null)
      and cut captions.srt / captions.vtt against those estimates.

  python captions_v2.py --from-audio
      The human's recordings have landed in audio-user/sectionN.{wav,m4a,mp3}.
      For each: convert to wav, trim leading/trailing silence (silenceremove,
      ~-38dB, ~200ms pad) into audio-user/trimmed/sectionN.wav, measure the
      real duration, concat into audio-user/narration.wav (no gaps), write
      audio-user/timings.json, and regenerate both caption files against the
      *measured* durations. Prints a per-section table with the <3:00 guard.

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

WPM = 145.0
WPS = WPM / 60.0                # 2.41667 words / second
TAIL = 3.5                      # end-card still appended after §8

# caption shape
MAX_LINE = 42                   # chars per caption line
MAX_CUE_CHARS = 84              # <= 2 lines
MIN_CUE = 2.0                   # seconds
MAX_CUE = 6.0                   # seconds

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
def _wrap_lines(phrase: str) -> list[str]:
    """Greedily wrap a phrase's words into lines of <= MAX_LINE chars each.

    Guarantees every line fits, splitting a pathologically long single token
    if one ever appears (none do in this script).
    """
    lines: list[str] = []
    cur = ""
    for w in phrase.split():
        while len(w) > MAX_LINE:
            if cur:
                lines.append(cur)
                cur = ""
            lines.append(w[:MAX_LINE])
            w = w[MAX_LINE:]
        if not cur:
            cur = w
        elif len(cur) + 1 + len(w) <= MAX_LINE:
            cur += " " + w
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def build_cues(raw: str) -> list[str]:
    """Split a section's raw narration into cue strings.

    Each cue is <= 2 lines, each line <= MAX_LINE chars. Hard breaks at `//`
    (pauses): lines from different phrases never share a cue, so every pause
    starts a fresh caption.
    """
    phrases = [clean_inline(p) for p in raw.split("//")]
    phrases = [p for p in phrases if p]
    cues: list[str] = []
    for ph in phrases:
        lines = _wrap_lines(ph)
        for i in range(0, len(lines), 2):
            cues.append("\n".join(lines[i:i + 2]))
    return cues


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
def do_estimate(sections: list[dict]) -> None:
    durations = [s["words"] / WPS for s in sections]
    files = [None] * len(sections)
    write_timings(sections, durations, files)
    cues = build_cue_timeline(sections, durations)
    write_srt(cues, SRT)
    write_vtt(cues, VTT)
    print(f"wrote {TIMINGS}")
    print(f"wrote {SRT}  /  {VTT}")
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
    write_srt(cues, SRT)
    write_vtt(cues, VTT)
    print(f"wrote {NARRATION}  ({probe_duration(NARRATION):.1f}s)")
    print(f"wrote {TIMINGS}")
    print(f"wrote {SRT}  /  {VTT}")
    report_and_guard(sections, durations, len(cues), "from-audio")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--estimate", action="store_true",
                   help="no audio yet: estimate durations at 145 wpm")
    g.add_argument("--from-audio", action="store_true",
                   help="measure real durations from audio-user/sectionN.*")
    args = ap.parse_args()

    sections = parse_script(SCRIPT)
    if len(sections) != 8:
        sys.exit(f"expected 8 sections in {SCRIPT.name}, found {len(sections)}")

    if args.estimate:
        do_estimate(sections)
    else:
        do_from_audio(sections)


if __name__ == "__main__":
    main()

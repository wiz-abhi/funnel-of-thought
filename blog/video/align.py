"""Force-align the script to the recorded narration, and time everything off it.

Proportional timing (words-per-second) drifts: the speaker slows on a hard
sentence and races through a familiar one, so captions that were *distributed*
across a section slide out of sync with the voice even when the section
boundaries are right. This replaces estimation with measurement.

  1. ASR (faster-whisper small.en) transcribes the FINAL processed audio with
     word-level timestamps. It must be the final audio — pauses squeezed and
     tempo applied — so the timestamps map onto what actually ships.
  2. The ASR transcript is aligned to the true script with difflib, which
     absorbs mishearings ("Signals" for "SigNoz") and dropped filler.
  3. Every script word inherits a real timestamp: matched words directly,
     unmatched ones interpolated between their nearest matched anchors.
  4. Section boundaries and caption cues are then read off actual word times,
     so a cue appears exactly when its first word is spoken.

    python align.py
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from difflib import SequenceMatcher
from pathlib import Path

HERE = Path(__file__).parent
AUD = HERE / "audio-user"
WORDS = AUD / "words.json"
FINAL = AUD / "final-narration.wav"
SCRIPT = HERE / "RECORDING-SCRIPT.md"

sys.path.insert(0, str(HERE))
import captions_v2 as C  # noqa: E402


def norm(w: str) -> str:
    """Comparison key: lowercase, letters/digits only."""
    return re.sub(r"[^a-z0-9]", "", w.lower())


_ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight",
         "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen",
         "sixteen", "seventeen", "eighteen", "nineteen"]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy",
         "eighty", "ninety"]


def say_number(n: int) -> list[str]:
    """125 -> ['one','hundred','twenty','five'] — the words a reader says."""
    out: list[str] = []
    if n >= 100:
        out += [_ONES[n // 100], "hundred"]
        n %= 100
    if n >= 20:
        out.append(_TENS[n // 10])
        n %= 10
        if n:
            out.append(_ONES[n])
    elif n:
        out.append(_ONES[n])
    return out


def expand_numerics(asr: list[dict]) -> list[dict]:
    """Rewrite digit tokens as the words that were actually spoken.

    Whisper emits "125", "64%", "36 %" while the script reads "one hundred and
    twenty-five", "sixty-four percent". Those tokens can never string-match, so
    every number in the script fell back to interpolation — and the numbers are
    exactly the cues this video is about. Expanding a numeric token into its
    spoken words, each taking an equal slice of that token's timespan, lets the
    aligner anchor them like any other word.
    """
    out: list[dict] = []
    for tok in asr:
        m = re.fullmatch(r"(\d+)\s*(%?)", tok["w"].strip().rstrip(".,"))
        if not m:
            out.append(tok)
            continue
        words = say_number(int(m.group(1)))
        if m.group(2):
            words.append("percent")
        span = (tok["e"] - tok["s"]) / max(len(words), 1)
        for i, w in enumerate(words):
            out.append({"w": w, "s": tok["s"] + i * span,
                        "e": tok["s"] + (i + 1) * span})
    return out


def main() -> None:
    asr = expand_numerics(json.loads(WORDS.read_text(encoding="utf-8")))
    secs = C.parse_script(SCRIPT)

    # ---- flatten the script into words tagged with (section, cue) ----------
    flat: list[dict] = []
    per_section_cues: list[list[str]] = []
    for si, s in enumerate(secs):
        cues = C.build_cues(s["raw"])
        per_section_cues.append(cues)
        for ci, cue in enumerate(cues):
            for w in C.strip_tags(cue).split():
                if norm(w):
                    flat.append({"w": w, "si": si, "ci": ci})

    a = [norm(x["w"]) for x in flat]
    b = [norm(x["w"]) for x in asr]

    # ---- align, then give every script word a real time -------------------
    sm = SequenceMatcher(a=a, b=b, autojunk=False)
    for x in flat:
        x["t"] = None
        x["te"] = None
    matched = 0
    for i, j, n in sm.get_matching_blocks():
        for k in range(n):
            flat[i + k]["t"] = asr[j + k]["s"]
            flat[i + k]["te"] = asr[j + k]["e"]
            matched += 1

    # interpolate the gaps between anchors
    anchors = [i for i, x in enumerate(flat) if x["t"] is not None]
    if not anchors:
        sys.exit("alignment failed: no matching words")
    first, last = anchors[0], anchors[-1]
    for i in range(first):
        flat[i]["t"] = flat[first]["t"]
        flat[i]["te"] = flat[first]["t"]
    for i in range(last + 1, len(flat)):
        flat[i]["t"] = flat[last]["te"]
        flat[i]["te"] = flat[last]["te"]
    for p, q in zip(anchors, anchors[1:]):
        if q - p > 1:
            t0, t1 = flat[p]["te"], flat[q]["t"]
            for k in range(p + 1, q):
                f = (k - p) / (q - p)
                flat[k]["t"] = t0 + (t1 - t0) * f
                flat[k]["te"] = t0 + (t1 - t0) * (f + 1 / (q - p))

    cov = matched / len(flat) * 100
    print(f"aligned {matched}/{len(flat)} script words directly ({cov:.1f}% anchored), "
          f"rest interpolated")
    if cov < 60:
        print("  WARNING: low anchor coverage — check the transcript")

    # ---- cue timings straight off the words --------------------------------
    cues_out: list[dict] = []
    for si, cues in enumerate(per_section_cues):
        for ci, cue in enumerate(cues):
            ws = [x for x in flat if x["si"] == si and x["ci"] == ci]
            if not ws:
                continue
            cues_out.append({"text": cue, "start": ws[0]["t"], "end": ws[-1]["te"],
                             "si": si})

    # tidy: no overlaps, sane minimum on screen, hold to the next cue
    for i, c in enumerate(cues_out):
        nxt = cues_out[i + 1]["start"] if i + 1 < len(cues_out) else c["end"] + 0.6
        c["end"] = max(c["end"] + 0.12, min(c["start"] + 1.2, nxt - 0.02))
        c["end"] = min(c["end"], nxt - 0.02) if nxt - 0.02 > c["start"] else c["start"] + 0.5

    # ---- section durations from the first word of each section -------------
    total = float(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(FINAL)], capture_output=True, text=True).stdout.strip())
    starts = []
    for si in range(len(secs)):
        ws = [x for x in flat if x["si"] == si]
        starts.append(ws[0]["t"] if ws else 0.0)
    starts[0] = 0.0
    bounds = starts + [total]

    timings = {"mode": "aligned", "sections": [], "total_duration": round(total, 3)}
    print(f"\n{'sec':>4} {'start':>8} {'dur':>8}  first words")
    for si, s in enumerate(secs):
        d = bounds[si + 1] - bounds[si]
        head = " ".join(C.strip_tags(per_section_cues[si][0]).split()[:6])
        print(f"{s['n']:>4} {bounds[si]:8.2f} {d:8.2f}  {head}")
        timings["sections"].append({"n": s["n"], "file": "audio-user/final-narration.wav",
                                    "duration": round(d, 3), "target": s.get("target", 0),
                                    "delta": 0})
    (AUD / "timings.json").write_text(json.dumps(timings, indent=1), encoding="utf-8")

    # ---- write the caption files with real timings -------------------------
    # write_captions() also runs the single-line / no-overlap / no-tags asserts
    C.write_captions(cues_out)
    print(f"\n{len(cues_out)} cues written from measured word times")
    print(f"video total {total + 3.5:.1f}s ({int((total+3.5)//60)}:{(total+3.5)%60:04.1f})")


if __name__ == "__main__":
    main()

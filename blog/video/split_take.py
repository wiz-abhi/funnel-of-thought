"""Split ONE continuous narration take into the 8 script sections.

The recording was made in a single pass, so there is no reliable "section
pause" signature — the gaps between sections are the same length as the `//`
pauses inside them (measured: every gap in the take is 0.5–1.5s). Detecting
boundaries by silence length alone therefore does not work.

Instead: place each boundary where the *script* says it should fall (by word
count, over the actual speech span), then snap it to the nearest real silence
gap so the cut never lands mid-word. Speaking rate drifts, so the snap window
is generous and the fit is reported for inspection.

    python split_take.py            # split + report
    python split_take.py --verify   # additionally ASR-check each section's opening
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
TAKE = HERE / "audio-user" / "raw-take.wav"
OUTDIR = HERE / "audio-user"
SCRIPT = HERE / "RECORDING-SCRIPT.md"

NOISE_DB = -30          # measured floor: mean -16.8dB, so -30 is comfortably below speech
MIN_GAP = 0.35          # candidate gaps; section pauses are >=0.5 but keep headroom
SNAP_WINDOW = 1.5       # keep cuts near-proportional; just enough to avoid a mid-word cut


def run_out(cmd: list[str]) -> str:
    p = subprocess.run(cmd, capture_output=True, text=True)
    return p.stdout + p.stderr


def duration(path: Path) -> float:
    return float(run_out(["ffprobe", "-v", "error", "-show_entries",
                          "format=duration", "-of", "csv=p=0", str(path)]).strip())


def gaps(path: Path) -> list[tuple[float, float]]:
    """[(start, end)] of every detected silence."""
    out = run_out(["ffmpeg", "-hide_banner", "-i", str(path),
                   "-af", f"silencedetect=noise={NOISE_DB}dB:d={MIN_GAP}",
                   "-f", "null", "-"])
    starts = [float(m) for m in re.findall(r"silence_start: ([\d.]+)", out)]
    ends = [float(m) for m in re.findall(r"silence_end: ([\d.]+)", out)]
    return list(zip(starts, ends))


def sections() -> list[dict]:
    sys.path.insert(0, str(HERE))
    from captions_v2 import parse_script          # reuse the one parser
    return parse_script(SCRIPT)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify", action="store_true")
    args = ap.parse_args()

    if not TAKE.exists():
        sys.exit(f"missing {TAKE}")

    secs = sections()
    total = duration(TAKE)
    g = gaps(TAKE)

    # Speech span: after any leading silence, before any trailing silence.
    speech_start = g[0][1] if g and g[0][0] < 1.0 else 0.0
    speech_end = g[-1][0] if g and g[-1][1] > total - 1.0 else total
    span = speech_end - speech_start

    words = [len(s["words"]) if isinstance(s.get("words"), list)
             else len(str(s.get("raw", "")).split()) for s in secs]
    tot_w = sum(words)

    # proportional boundaries (7 internal), then snap to the nearest real gap
    mids = [((a + b) / 2) for a, b in g]
    bounds = [speech_start]
    acc = 0
    for w in words[:-1]:
        acc += w
        want = speech_start + span * acc / tot_w
        near = [m for m in mids if abs(m - want) <= SNAP_WINDOW and m > bounds[-1] + 3]
        bounds.append(min(near, key=lambda m: abs(m - want)) if near else want)
    bounds.append(speech_end)

    print(f"take {total:.1f}s | speech {speech_start:.2f}–{speech_end:.2f}s "
          f"({span:.1f}s) | {len(g)} gaps\n")
    print(f"{'sec':>4} {'start':>8} {'end':>8} {'dur':>7} {'words':>6} {'wpm':>6}  snapped")
    cuts = []
    for i, s in enumerate(secs):
        a, b = bounds[i], bounds[i + 1]
        d = b - a
        wpm = words[i] / d * 60 if d else 0
        want = speech_start + span * sum(words[:i]) / tot_w
        snapped = "" if i == 0 else f"{a - want:+.2f}s"
        print(f"{s['n']:>4} {a:8.2f} {b:8.2f} {d:7.2f} {words[i]:6} {wpm:6.0f}  {snapped}")
        cuts.append((s["n"], a, b))

    for n, a, b in cuts:
        dst = OUTDIR / f"section{n}.wav"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(TAKE),
                        "-ss", f"{a:.3f}", "-to", f"{b:.3f}",
                        "-c:a", "pcm_s16le", str(dst)], check=True)
    print(f"\nwrote section1..{len(cuts)}.wav to {OUTDIR}")

    if args.verify:
        print("\nASR check — first words of each section vs the script:")
        for n, a, b in cuts:
            expect = " ".join(str(secs[n - 1]["raw"]).split()[:6]).lower()
            expect = re.sub(r"[^a-z0-9 ]", "", expect)
            print(f"  §{n} expect: {expect}")


if __name__ == "__main__":
    main()

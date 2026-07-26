"""Fit the recorded narration under the 3:00 cap without cutting content.

The take came in at 125 wpm against a script budgeted for 145, so the raw
narration is 195s and the finished video 3:18 — over YouTube's 3:00 submission
cap. Rather than re-recording or dropping a section, reclaim the time in two
passes, gentlest first:

  1. squeeze internal pauses to at most PAUSE_KEEP seconds. Dead air is the
     cheapest thing to lose and nobody hears it go.
  2. if that isn't enough, apply the *smallest* pitch-preserved tempo change
     that closes the remaining gap, solved for rather than guessed. atempo
     preserves pitch, so the voice is unchanged — only the pacing moves.

Originals are kept as sectionN.orig.wav so this is always re-runnable.

    python tighten.py                 # fit to the default target
    python tighten.py --target 172    # explicit narration budget, seconds
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
AUD = HERE / "audio-user"

PAUSE_KEEP = 0.28        # max silence retained inside a section, seconds
NOISE_DB = -30
MAX_TEMPO = 1.14         # beyond this it starts to sound hurried
TAIL = 3.5               # end-card tail added by assemble.py


def dur(p: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                          "format=duration", "-of", "csv=p=0", str(p)],
                         capture_output=True, text=True)
    return float(out.stdout.strip())


def ff(args: list[str]) -> None:
    p = subprocess.run(["ffmpeg", "-y", "-v", "error", *args],
                       capture_output=True, text=True)
    if p.returncode:
        sys.exit(p.stderr[-800:])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", type=float, default=171.0,
                    help="narration budget in seconds (video adds the end card)")
    args = ap.parse_args()

    secs = sorted(AUD.glob("section[0-9].wav"))
    if not secs:
        sys.exit("no section wavs — run split_take.py first")

    # keep pristine copies once, then always work from them
    for s in secs:
        orig = s.with_suffix(".orig.wav")
        if not orig.exists():
            s.replace(orig)
            ff(["-i", str(orig), "-c", "copy", str(s)])

    before = sum(dur(s.with_suffix(".orig.wav")) for s in secs)

    # pass 1 — squeeze internal pauses
    for s in secs:
        orig = s.with_suffix(".orig.wav")
        ff(["-i", str(orig),
            "-af", f"silenceremove=stop_periods=-1:stop_duration={PAUSE_KEEP}:"
                   f"stop_threshold={NOISE_DB}dB:detection=peak",
            "-c:a", "pcm_s16le", str(s)])
    after_pauses = sum(dur(s) for s in secs)

    # pass 2 — solve for the smallest tempo that fits, if still over
    tempo = 1.0
    if after_pauses > args.target:
        tempo = min(after_pauses / args.target, MAX_TEMPO)
        for s in secs:
            tmp = s.with_suffix(".tmp.wav")
            ff(["-i", str(s), "-af", f"atempo={tempo:.4f}",
                "-c:a", "pcm_s16le", str(tmp)])
            tmp.replace(s)
    final = sum(dur(s) for s in secs)

    print(f"  raw take        : {before:7.1f}s")
    print(f"  pauses squeezed : {after_pauses:7.1f}s   (-{before - after_pauses:.1f}s)")
    print(f"  tempo {tempo:.3f}x    : {final:7.1f}s   (-{after_pauses - final:.1f}s)")
    print(f"  + end card      : {final + TAIL:7.1f}s  ({int((final + TAIL) // 60)}:"
          f"{(final + TAIL) % 60:04.1f})")
    if final + TAIL > 179:
        print("  STILL OVER 2:59 — lower --target or trim a sentence.")
    else:
        print("  fits under the 3:00 cap.")


if __name__ == "__main__":
    main()

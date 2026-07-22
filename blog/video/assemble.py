"""Assemble the demo video.

Conforms every visual segment to the MEASURED narration duration for its
section (audio/timings.json), concatenates, muxes the narration track, and
burns in captions.

The audio is the master clock: SAPI section lengths never match the script's
estimates exactly, and drifting captions look far worse than a shot that
holds half a second longer.

    python assemble.py            # full build
    python assemble.py --check    # report what's present/missing and exit
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
SEG = HERE / "segments"
AUD = HERE / "audio"
CARDS = HERE / "cards"
ASSETS = HERE.parent / "assets"
BUILD = HERE / "build"
OUT = HERE / "funnel-of-thought-demo.mp4"

W, H, FPS = 1920, 1080, 30

# section -> visual source. Sections 1-2 are stills; 3-8 are live captures.
# Stills are listed as a sequence: each gets an equal share of the section.
PLAN: dict[int, dict] = {
    1: {"stills": [CARDS / "title.png", ASSETS / "diagram-01-contract.png"]},
    2: {"stills": [ASSETS / "meme-01-two-numbers.png"]},
    3: {"video": SEG / "shot3.mp4"},
    4: {"video": SEG / "shot4.mp4"},
    5: {"video": SEG / "shot5.mp4"},
    6: {"video": SEG / "shot6.mp4"},
    7: {"video": SEG / "shot7.mp4"},
    8: {"video": SEG / "shot8.mp4", "tail_still": CARDS / "end.png", "tail": 5.0},
}


def run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.exit(f"FAILED: {' '.join(cmd[:6])} …\n{proc.stderr[-1500:]}")


def probe_duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    )
    return float(out.stdout.strip())


def still_to_clip(img: Path, seconds: float, dst: Path) -> None:
    """A still, letterboxed to 1080p, held for `seconds`."""
    run([
        "ffmpeg", "-y", "-loop", "1", "-t", f"{seconds:.3f}", "-i", str(img),
        "-vf", f"scale={W}:{H}:force_original_aspect_ratio=decrease,"
               f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=#0f1720,fps={FPS},format=yuv420p",
        "-c:v", "libx264", "-preset", "medium", "-crf", "18", str(dst),
    ])


def conform(src: Path, seconds: float, dst: Path) -> None:
    """Trim or freeze-frame-extend a clip to exactly `seconds`."""
    have = probe_duration(src)
    if have >= seconds:
        run(["ffmpeg", "-y", "-i", str(src), "-t", f"{seconds:.3f}",
             "-vf", f"scale={W}:{H},fps={FPS},format=yuv420p",
             "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-an", str(dst)])
    else:
        pad = seconds - have
        run(["ffmpeg", "-y", "-i", str(src),
             "-vf", f"scale={W}:{H},fps={FPS},format=yuv420p,"
                    f"tpad=stop_mode=clone:stop_duration={pad:.3f}",
             "-t", f"{seconds:.3f}",
             "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-an", str(dst)])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    timings_path = AUD / "timings.json"
    missing = []
    if not timings_path.exists():
        missing.append(str(timings_path))
    for n, spec in PLAN.items():
        for key in ("video", "tail_still"):
            if key in spec and not spec[key].exists():
                missing.append(str(spec[key]))
        for s in spec.get("stills", []):
            if not s.exists():
                missing.append(str(s))
    narration = AUD / "narration.wav"
    if not narration.exists():
        missing.append(str(narration))

    if args.check or missing:
        print("MISSING:" if missing else "all inputs present")
        for m in missing:
            print("  -", m)
        if args.check:
            return
        sys.exit(1)

    timings = json.loads(timings_path.read_text())
    by_n = {s["n"]: s for s in timings["sections"]}

    BUILD.mkdir(exist_ok=True)
    parts: list[Path] = []

    for n in sorted(PLAN):
        spec = PLAN[n]
        seconds = float(by_n[n]["duration"])
        if "stills" in spec:
            share = seconds / len(spec["stills"])
            for i, img in enumerate(spec["stills"]):
                dst = BUILD / f"s{n}_{i}.mp4"
                still_to_clip(img, share, dst)
                parts.append(dst)
        else:
            dst = BUILD / f"s{n}.mp4"
            conform(spec["video"], seconds, dst)
            parts.append(dst)
            if "tail_still" in spec:
                tail = BUILD / f"s{n}_tail.mp4"
                still_to_clip(spec["tail_still"], float(spec["tail"]), tail)
                parts.append(tail)

    listfile = BUILD / "concat.txt"
    listfile.write_text("\n".join(f"file '{p.as_posix()}'" for p in parts))

    silent = BUILD / "silent.mp4"
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(listfile),
         "-c", "copy", str(silent)])

    # Mux narration. Video may run longer than audio (the end card tail) — keep
    # the video and let the audio finish early rather than truncating the card.
    withaudio = BUILD / "withaudio.mp4"
    run(["ffmpeg", "-y", "-i", str(silent), "-i", str(narration),
         "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", str(withaudio)])

    srt = HERE / "captions.srt"
    if srt.exists():
        style = ("FontName=Segoe UI,FontSize=22,PrimaryColour=&H00F4EEE8,"
                 "OutlineColour=&H00201710,BorderStyle=3,Outline=1,Shadow=0,"
                 "MarginV=48,Alignment=2")
        run(["ffmpeg", "-y", "-i", str(withaudio),
             "-vf", f"subtitles={srt.as_posix()}:force_style='{style}'",
             "-c:v", "libx264", "-preset", "medium", "-crf", "18",
             "-c:a", "copy", str(OUT)])
    else:
        shutil.copy(withaudio, OUT)
        print("NOTE: captions.srt absent — shipped without burned-in captions")

    print(f"\nwrote {OUT}  ({probe_duration(OUT):.1f}s)")


if __name__ == "__main__":
    main()

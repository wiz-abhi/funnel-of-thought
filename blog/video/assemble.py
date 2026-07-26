"""Assemble the demo video (v2).

Conforms every visual segment to the section duration (audio-user/timings.json
if present, else the v1 audio/timings.json reference), concatenates, muxes the
narration track *if it exists*, and burns in captions.

The audio is the master clock: recorded section lengths never match the
script's estimates exactly, and drifting captions look far worse than a shot
that holds half a second longer. Until the human's recordings arrive there is
no narration — the build is then SILENT but still fully captioned (preview
cut), driven by the --estimate durations in audio-user/timings.json.

Guard: the finished video must stay < 3:00 (YouTube submission cap). Warn
above 2:55, hard-fail above 2:59.

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
AUD = HERE / "audio"                 # v1 TTS reference (never muxed in v2)
USER = HERE / "audio-user"           # v2: human recordings + generated timings
CARDS = HERE / "cards"
ASSETS = HERE.parent / "assets"
BUILD = HERE / "build"
OUT = HERE / "funnel-of-thought-demo.mp4"

W, H, FPS = 1920, 1080, 30

# <3:00 guard, measured on the finished file.
WARN_S = 2 * 60 + 55                 # 175 -> warn
FAIL_S = 2 * 60 + 59                 # 179 -> hard fail

# section -> visual source (v3 mapping).
#
# The split is deliberate: **hand-drawn sketch animation carries the
# explanation, real captured footage carries the evidence.** A judge should
# never be shown a drawing where a real terminal or a real SigNoz page could
# make the same point.
#
#   "seq" = an ordered mix of stills (.png) and clips (.mp4), each taking the
#   given fraction of the section's duration (fractions must sum to 1.0).
PLAN: dict[int, dict] = {
    # intro: title card -> the contract sketching itself -> what it ships
    1: {"seq": [(CARDS / "title.png", 0.13),
                (SEG / "intro-sketch.mp4", 0.62),
                (CARDS / "ships.png", 0.25)]},
    # the two numbers, shown as the real counter-proof output
    2: {"video": SEG / "shot5.mp4"},
    3: {"video": SEG / "shot3.mp4"},          # fot show — the cliff
    4: {"video": SEG / "shot4.mp4"},          # SigNoz's own Funnels UI
    # why a counter can't see it: explanation, so the sketch earns its place
    5: {"video": SEG / "counter-sketch.mp4"},
    6: {"video": SEG / "shot6.mp4"},          # the violating trace waterfall
    7: {"video": SEG / "shot7.mp4"},          # the agent reads its own funnel
    8: {"video": SEG / "shot8.mp4", "tail_still": CARDS / "end.png", "tail": 3.5},
}


def pick_timings() -> Path | None:
    """Prefer the v2 audio-user/timings.json; fall back to v1 reference."""
    if (USER / "timings.json").exists():
        return USER / "timings.json"
    if (AUD / "timings.json").exists():
        return AUD / "timings.json"
    return None


def run(cmd: list[str], cwd: Path | None = None) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)
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
    """A still held for `seconds`, with a reserved caption band.

    Full-bleed 1920x1080 stills (the diagrams/cards) collide with burned-in
    captions — a caption landed exactly on diagram-01's own footer text and
    garbled both. So stills are scaled to 940px tall and top-padded, leaving a
    ~110px clean band at the bottom that the caption strip (MarginV=56 in real
    pixels, ~74px tall) lands inside. Video segments keep their native
    letterbox.

    The pad colour must track the card theme: the cards are #08090C, and
    padding them with the old flat #0f1720 framed each one in a visibly
    lighter blue-grey bar.
    """
    run([
        "ffmpeg", "-y", "-loop", "1", "-t", f"{seconds:.3f}", "-i", str(img),
        "-vf", f"scale={W}:{H - 140}:force_original_aspect_ratio=decrease,"
               f"pad={W}:{H}:(ow-iw)/2:30:color=#08090C,fps={FPS},format=yuv420p",
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

    timings_path = pick_timings()
    missing = []
    if timings_path is None:
        missing.append(str(USER / "timings.json") + " (run: captions_v2.py --estimate)")
    for n, spec in PLAN.items():
        for key in ("video", "tail_still"):
            if key in spec and not spec[key].exists():
                missing.append(str(spec[key]))
        for s in [p for p,_ in spec.get("seq", [])] + spec.get("stills", []):
            if not s.exists():
                missing.append(str(s))

    # narration is OPTIONAL in v2: absent -> silent preview (still captioned).
    narration = USER / "narration.wav"
    have_audio = narration.exists()

    if args.check or missing:
        print("MISSING:" if missing else "all required inputs present")
        for m in missing:
            print("  -", m)
        if args.check:
            print(f"timings   : {timings_path if timings_path else '(none)'}")
            print(f"narration : {'audio-user/narration.wav' if have_audio else 'NONE -> silent build'}")
            return
        sys.exit(1)

    print(f"timings   : {timings_path}")
    print(f"narration : {'audio-user/narration.wav' if have_audio else 'NONE -> SILENT preview build'}")
    timings = json.loads(timings_path.read_text(encoding="utf-8"))
    by_n = {s["n"]: s for s in timings["sections"]}

    BUILD.mkdir(exist_ok=True)
    parts: list[Path] = []

    for n in sorted(PLAN):
        spec = PLAN[n]
        seconds = float(by_n[n]["duration"])
        if "seq" in spec:
            # ordered mix of stills and clips, each taking its fraction
            for i, (src, frac) in enumerate(spec["seq"]):
                dst = BUILD / f"s{n}_{i}.mp4"
                slot = seconds * float(frac)
                if src.suffix.lower() == ".png":
                    still_to_clip(src, slot, dst)
                else:
                    conform(src, slot, dst)
                parts.append(dst)
        elif "stills" in spec:
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

    # Mux narration only if the human's recordings have been assembled. Video
    # may run longer than audio (the end card tail) — keep the video and let the
    # audio finish early rather than truncating the card. No narration -> the
    # captioning step reads the silent concat directly (preview cut).
    if have_audio:
        base = BUILD / "withaudio.mp4"
        run(["ffmpeg", "-y", "-i", str(silent), "-i", str(narration),
             "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", str(base)])
    else:
        base = silent

    # captions.ass is the real deal: PlayRes 1920x1080 so sizes are pixels,
    # one line per cue, a translucent padded strip (BorderStyle=3) behind the
    # text, and inline colour on the key words. It carries its own styling, so
    # no force_style here. The SRT is only a fallback (and the a11y side-car).
    #
    # ffmpeg's subtitles/ass filter splits options on ':', so a Windows drive
    # letter ("C:/…") is misread as an option. Run from the file's own
    # directory and pass a bare filename instead of escaping.
    ass = HERE / "captions.ass"
    srt = HERE / "captions.srt"
    if ass.exists():
        vf = f"ass={ass.name}"
        cap = "captions.ass (styled strip)"
    elif srt.exists():
        style = ("FontName=Segoe UI,FontSize=17,PrimaryColour=&H00F4EEE8,"
                 "OutlineColour=&H00201710,BorderStyle=3,Outline=1,Shadow=0,"
                 "MarginV=10,Alignment=2")
        vf = f"subtitles={srt.name}:force_style='{style}'"
        cap = "captions.srt (fallback — run captions_v2.py to get the ASS)"
    else:
        vf = None
        cap = None

    if vf:
        print(f"captions  : {cap}")
        cmd = ["ffmpeg", "-y", "-i", str(base), "-vf", vf,
               "-c:v", "libx264", "-preset", "medium", "-crf", "18"]
        cmd += ["-c:a", "copy"] if have_audio else ["-an"]
        cmd.append(str(OUT))
        run(cmd, cwd=HERE)
    else:
        shutil.copy(base, OUT)
        print("NOTE: no captions.ass/.srt — shipped without burned-in captions")

    dur = probe_duration(OUT)
    kind = "with narration" if have_audio else "SILENT preview (captioned)"
    print(f"\nwrote {OUT}  ({dur:.1f}s, {int(dur)//60}:{int(round(dur))%60:02d}, {kind})")

    # <3:00 guard on the finished file.
    if dur > FAIL_S:
        sys.exit(f"GUARD FAIL: {dur:.1f}s > 2:59 — the finished video exceeds "
                 f"the 3:00 cap. Tighten the script or a section duration.")
    if dur > WARN_S:
        print(f"GUARD WARN: {dur:.1f}s > 2:55 — under 3:00 but the margin is thin.")
    else:
        print(f"GUARD OK: {dur:.1f}s < 2:55 — comfortably under the 3:00 cap.")


if __name__ == "__main__":
    main()

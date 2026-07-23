# When the narration recordings arrive

The current `funnel-of-thought-demo.mp4` is a **silent, captioned PREVIEW cut**
built from 145-wpm duration *estimates*. When you record the narration, this
turns it into the final cut with your real voice and captions re-timed to it.

## Drop the files

Record **one file per section, 8 files**, named exactly:

```
blog/video/audio-user/section1.wav   ...   section8.wav
```

`.m4a` and `.mp3` are also accepted. Mono, 44.1/48 kHz, quiet room.
`//` in `RECORDING-SCRIPT.md` = pause ~0.5s; don't say it.

## Run these 3 commands (from `blog/video/`)

```bash
# 0. use the repo venv + UTF-8 (PowerShell mangles the em dashes otherwise)
export PYTHONIOENCODING=utf-8              # PowerShell: $env:PYTHONIOENCODING="utf-8"
PY=../../.venv/Scripts/python.exe

# 1. trim silences, measure real durations, build narration.wav, re-cut captions
$PY captions_v2.py --from-audio

# 2. rebuild the video WITH your narration muxed in + captions re-burned
$PY assemble.py

# 3. confirm the finished file (duration, streams, <3:00)
ffprobe -v error -show_entries format=duration -show_entries stream=codec_type,width,height,r_frame_rate -of default=noprint_wrappers=1 funnel-of-thought-demo.mp4
```

## What each step produces

**Step 1 — `captions_v2.py --from-audio`**
- Converts each `sectionN.*` to wav, trims leading/trailing silence
  (~-38 dB, keeps ~200 ms pad) → `audio-user/trimmed/sectionN.wav`
- Measures the real trimmed durations
- Writes `audio-user/timings.json` (real durations, `file` set to the trimmed path)
- Writes `audio-user/narration.wav` (all 8 trimmed sections concatenated, no gaps)
- Regenerates `captions.srt` + `captions.vtt` re-timed to your voice
- Prints a per-section table + total, and applies the guard:
  - **> 2:55 → WARN** (under 3:00 but thin — consider a tighter re-read)
  - **> 2:59 → hard FAIL** (exits; re-record the slowest section, don't trim content)

**Step 2 — `assemble.py`**
- Auto-detects `audio-user/timings.json` (preferred over the old
  `audio/timings.json`) and `audio-user/narration.wav`
- Conforms each shot to its section's measured duration (trim-from-end or
  freeze-pad), concats, **muxes `narration.wav`**, burns captions
- Writes `funnel-of-thought-demo.mp4` and re-checks the guard on the finished file
- (If `narration.wav` is absent it silently falls back to the silent preview.)

**Step 3 — `ffprobe`**
- Expect: `1920x1080`, `h264`, `30/1` fps, **one video + one aac audio stream**,
  and a `format=duration` **under 180 s (3:00)**. Estimated preview is ~166.5 s
  (2:46); the real read will differ but must stay under 3:00.

## The one hard rule

**The finished YouTube video must be under 3:00.** The pipeline warns at 2:55
and hard-fails at 2:59 in both `captions_v2.py` and `assemble.py`. If it fails,
re-record the slowest section rather than cutting content — the captions and
shot conform re-derive automatically from the new durations on the next run.

## Notes

- `audio/` (v1 SAPI TTS) is kept only as a schema reference and is **never**
  muxed into the v2 build. The v1 render is archived at
  `archive/funnel-of-thought-demo-v1-tts.mp4`.
- To rebuild the silent preview at any time (no recordings needed):
  `$PY captions_v2.py --estimate && $PY assemble.py`
- `build/` and `frames/` are throwaway (gitignored).

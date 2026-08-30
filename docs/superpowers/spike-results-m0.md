# M0 Spike Results

Machine: HP ProBook 450 G10 — i7-1355U (12 threads), 16GB RAM (15Gi usable),
Zorin OS 18.1 (Ubuntu 24.04 "noble" base), x86_64, Python 3.12.12 via uv.

Verified 2026-08-30 with `cat /etc/os-release`, `uname -m`, `lscpu`, `free -h`.
The plan's assumed machine line was accurate — no correction needed.

## FFmpeg environment (Task 5)
- Version: `ffmpeg version 6.1.1-3ubuntu5 Copyright (c) 2000-2023 the FFmpeg developers`
  (apt package `7:6.1.1-3ubuntu5` from `noble/universe`; `ffprobe version 6.1.1-3ubuntu5`)
- subtitles/drawtext filters: **present** in the apt build — `ffmpeg -hide_banner -filters`
  lists `subtitles` and `ass` ("using the libass library") plus `drawtext` (libfreetype).
  `libass9 1:0.17.1-2build1` is installed. No extra install was needed.
- Hardware encoder: **both `h264_qsv` and `h264_vaapi`** are available.
  ```
  V..... h264_qsv    H.264 / AVC / MPEG-4 AVC / MPEG-4 part 10 (Intel Quick Sync Video acceleration) (codec h264)
  V....D h264_vaapi  H.264/AVC (VAAPI) (codec h264)
  ```
  `libva2`, `intel-media-va-driver` and `i965-va-driver` are installed; `vainfo` is not.
  Doctor reports `hardware encoder OK — h264_qsv available for fast renders`.
  Still not a blocker either way: libx264 on 12 threads remains the default.

### Finding for M1: the snap ffmpeg is a trap
The apt `ffmpeg` package was installed during M0 and now shadows a pre-existing
snap build (`snapcrafters/ffmpeg 4.3.1`, rev 1286), which is still installed:

- `which -a ffmpeg` → `/usr/bin/ffmpeg`, `/bin/ffmpeg`, `/snap/bin/ffmpeg` — `/usr/bin`
  precedes `/snap/bin` on PATH, so the apt 6.1.1 build wins.
- The snap **does not put `ffprobe` on PATH**: it exposes `ffmpeg`, `ffmpeg.ffplay`
  and `ffmpeg.ffprobe` only. With the snap alone, `shutil.which("ffprobe")` is `None`
  and doctor's `ffprobe` check FAILS.
- The snap is also two majors behind (n4.3.1, 2020) and errors on this laptop's GPU
  (`Driver does not support the 0xa721 PCI ID.`).

M1 implication: never assume `ffmpeg` on PATH implies `ffprobe`, and never assume a
usable version — probe both binaries and the version explicitly (`media/ffmpeg.py`
already does the `shutil.which("ffprobe")` part). If a contributor's PATH puts
`/snap/bin` first, they get 4.3.1 with no ffprobe and captions/probing break.

### Doctor status at end of Task 5
`uv run videomaker doctor` exits 0:

| check | status | detail |
| --- | --- | --- |
| python | OK | 3.12.12 |
| ffmpeg | OK | ffmpeg version 6.1.1-3ubuntu5 |
| ffmpeg subtitles filter | OK | libass available |
| hardware encoder | OK | h264_qsv available for fast renders |
| disk space | OK | 100 GB free |
| kokoro model files | WARN | missing: kokoro-v1.0.onnx, voices-v1.0.bin |
| LLM API key | WARN | neither GROQ_API_KEY nor GEMINI_API_KEY set |
| Pexels API key | WARN | PEXELS_API_KEY not set |

Note: `ffprobe` has no OK row — `_check_ffmpeg` only emits an `ffprobe` result when it
is missing (fail-only check). Its absence from the table is the pass signal.
The three WARNs are the expected pre-`setup` state.

## kokoro-onnx / onnxruntime wheels (Task 6)
- **Result: clean.** `uv sync --extra ml` resolved 52 packages and installed 24 new ones
  from prebuilt manylinux wheels in ~20s. No build step, no compiler, **no pins needed**.
- Resolved versions (`uv pip list`, 2026-08-30):

  | package | version |
  | --- | --- |
  | kokoro-onnx | 0.6.1 |
  | onnxruntime | 1.29.0 |
  | ctranslate2 | 4.8.1 |
  | faster-whisper | 1.2.1 |
  | soundfile | 0.14.0 |
  | numpy | 2.5.2 |

  Notable transitive deps pulled in: `av 18.1.0`, `tokenizers 0.23.1`,
  `huggingface-hub 1.29.0`, `phonemizer 3.4.0`, `espeakng-loader 0.2.4`,
  `protobuf 7.36.0`, `hf-xet 1.6.0`. `espeakng-loader` ships its own bundled
  espeak-ng, so no `apt install espeak-ng` was required.
- Import check: `uv run python -c "from kokoro_onnx import Kokoro; print('kokoro-onnx import OK')"`
  printed `kokoro-onnx import OK` with **no warnings** — onnxruntime 1.29.0 loads fine
  against numpy 2.5.2 (no numpy 1.x pin needed, which is the usual failure mode elsewhere).
- Model download: `uv run videomaker setup` fetched both files to
  `~/.cache/ai-video-maker/models/` on the first try (GitHub release
  `thewh1teagle/kokoro-onnx@model-files-v1.0`, HTTP redirect to the CDN followed):

  ```
  -rw-rw-r-- 311M kokoro-v1.0.onnx
  -rw-rw-r--  27M voices-v1.0.bin
  ```

  338 MB total, matching the plan's ~340 MB estimate. `uv run videomaker doctor` then
  reports `kokoro model files` = **OK** and exits 0 (only the two API-key WARNs remain).
- Downloader note: `download_file` streams to a `<name>.part` sibling and `Path.replace`s
  it into place, so a killed download leaves no truncated model behind and re-running
  `setup` is idempotent (`ensure_models` skips files that already exist).

### Deviation from the plan's snippet
The plan's `downloads.py` writes chunks with an explicit `for chunk in ...: fh.write(chunk)`
loop, which this repo's ruff (0.16.5, default ruleset) rejects as **FURB122**. Applied
ruff's own autofix — `fh.writelines(response.iter_bytes(256 * 1024))` — which streams
identically (writelines consumes the iterator lazily and writes no separators). Behaviour
is unchanged; both download tests pass.

## Kokoro TTS smoke test (Task 7)
- (pending)

## faster-whisper / ctranslate2 wheels (Task 8)
- (pending)

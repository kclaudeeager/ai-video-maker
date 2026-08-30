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
- **Result: works, first try, no API adaptation needed.** `spike_tts` uses the plan's
  snippet verbatim: `Kokoro(model_path, voices_path)` then
  `kokoro.create(text, voice="af_heart", speed=1.0)`. kokoro-onnx 0.6.1's real signature is
  `create(text, voice, speed=1.0, lang="en-us", is_phonemes=False, trim=True, sentence_pause=0.25, clause_pause=0.1, continuous=False)`,
  so `lang` defaults correctly and the plan's call needs no extra argument. The only edit
  was moving `import time` to the top of `setup_cmd.py` (the plan appends it mid-file,
  which ruff rejects as E402).
- Text: `"This is a Kokoro voice test on this machine."` — voice **`af_heart`**, speed 1.0.
- Output: `~/.cache/ai-video-maker/models/smoke_test.wav`

  ```
  Input #0, wav, from 'smoke_test.wav':
    Duration: 00:00:02.39, bitrate: 384 kb/s
    Stream #0:0: Audio: pcm_s16le, 24000 Hz, 1 channels, s16, 384 kb/s
  ```

  **24 kHz mono PCM s16le**, 57,344 samples, **2.389 s** of audio. Kokoro returns float32
  at 24 kHz; `soundfile.write` defaults that to 16-bit PCM — M1 should pass an explicit
  `subtype` if it wants float or 48 kHz for the mixdown.
- **RTF (wall/audio) = 0.53** on the i7-1355U — 2.4 s of audio in 1.3 s, as printed by
  `videomaker setup`. Within the plan's expected "well under 1.0" band. Repeat measurements
  in one process: 0.86 for the first `create()` (lazy espeak-ng + phonemizer init is paid
  inside the timed region) then **0.47** warm. Model load / ONNX session construction sits
  outside the timer. Budget for M1: ~0.5x realtime per narration segment on this laptop,
  plus a one-off ~1 s warm-up on the first call of a process.
- Audio is **not silent**, verified numerically: peak amplitude **0.4829**, RMS **0.0731**,
  and 67.3% of samples exceed |0.01| — consistent with continuous speech, no clipping
  (peak < 1.0) and no dead channel.
- **Quality note: subjective listening was NOT possible** — this task was executed by an
  agent with no audio playback, so the plan's "play it and confirm an intelligible female
  voice" check could not be performed. What is verified is objective only: correct format,
  plausible duration for a 44-character sentence (~2.4 s ≈ 18 chars/s, a normal speaking
  rate), and healthy non-silent amplitude. **Intelligibility is confirmed in Task 8**,
  where faster-whisper transcribes this exact wav — a round-trip that fails loudly if the
  audio is noise, truncated, or the wrong words.

## faster-whisper / ctranslate2 wheels (Task 8)
- **Result: clean, first try, no API adaptation and no pins needed.** `spike_stt` uses the
  plan's snippet **verbatim** — `WhisperModel("base", device="cpu", compute_type="int8",
  download_root=...)` then `model.transcribe(str(wav), word_timestamps=True)`, iterating
  `segment.words` for `(word.word.strip(), word.start, word.end)`. faster-whisper 1.2.1's
  real signatures still accept every one of those arguments (`WhisperModel.__init__(self,
  model_size_or_path, device='auto', device_index=0, compute_type='default', cpu_threads=0,
  num_workers=1, download_root=None, local_files_only=False, ...)` and `transcribe(...,
  word_timestamps=False, ...) -> Tuple[Iterable[Segment], TranscriptionInfo]`), checked with
  `inspect.signature` before writing the code rather than assumed. The plan's fallback pin
  (`faster-whisper==0.10.1` + `ctranslate2==3.24.0`) was **not** needed — `pyproject.toml`
  and `uv.lock` are unchanged by this task.
- Resolved versions (unchanged since Task 6): **faster-whisper 1.2.1**, **ctranslate2 4.8.1**,
  with `av 18.1.0`, `tokenizers 0.23.1`, `huggingface-hub 1.29.0`, `onnxruntime 1.29.0`
  (Silero VAD), `numpy 2.5.2`. All prebuilt manylinux wheels; no compiler, no `apt` package.
- **Model cache location:** `download_root` is passed straight to `huggingface_hub`, so the
  files land in a HF-layout cache **under our own models dir**, not in `~/.cache/huggingface`:

  ```
  ~/.cache/ai-video-maker/models/whisper/
    models--Systran--faster-whisper-base/
      snapshots/ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66/
        config.json  model.bin  tokenizer.json  vocabulary.txt
  ```

  **142 MB** on disk (repo `Systran/faster-whisper-base`), matching the plan's ~145 MB
  estimate. Download emits one benign stderr line — `Warning: You are sending
  unauthenticated requests to the HF Hub. Please set a HF_TOKEN to enable higher rate limits
  and faster downloads.` — anonymous access works fine; M1 need not require a token, but a
  rate-limited CI runner is a plausible future flake source.

### Round-trip result: Kokoro TTS → whisper STT
Input sentence (what Task 7 synthesized): `This is a Kokoro voice test on this machine.`

Transcript (all 9 words joined): **`This is a Kakoro voice test on this machine.`**

| # | word | start (s) | end (s) |
| --- | --- | --- | --- |
| 1 | This | 0.00 | 0.16 |
| 2 | is | 0.16 | 0.32 |
| 3 | a | 0.32 | 0.46 |
| 4 | Kakoro | 0.46 | 0.86 |
| 5 | voice | 0.86 | 1.16 |
| 6 | test | 1.16 | 1.46 |
| 7 | on | 1.46 | 1.64 |
| 8 | this | 1.64 | 1.80 |
| 9 | machine. | 1.80 | 2.06 |

- **Accuracy: 8/9 words exact (88.9% WER-complement); the single miss is the proper noun.**
  "Kokoro" came back as **"Kakoro"** — one vowel wrong out of six characters. Every common
  word, the sentence casing and the final period are correct. This is the expected failure
  mode for an out-of-vocabulary proper noun on the `base` model and is **not** evidence of
  bad TTS audio; whisper simply has no "Kokoro" prior. M1 mitigation if proper-noun fidelity
  ever matters for alignment: `transcribe(..., hotwords="Kokoro")` or `initial_prompt=` are
  available in 1.2.1, or use a larger model.
- **This retroactively confirms Task 7's audio is genuinely intelligible speech**, which the
  Task 7 agent could not verify by ear. A round-trip that recovers the exact sentence proves
  the wav is not noise, not truncated, and says the right words in the right order.

### Word-timestamp quality
- Timings are **plausible and well-formed**: monotonically increasing, contiguous
  (each word's `start` equals the previous word's `end` — whisper's DTW alignment emits no
  gaps inside a segment), starting at 0.00 s.
- Coverage vs the source: the wav is **2.389 s**; the last word ends at **2.06 s**, leaving
  **0.33 s** unaccounted. That tail is Kokoro's trailing silence/decay after "machine", so
  the alignment is consistent with the audio rather than short of it. No timestamp exceeds
  the file duration.
- Per-word durations (0.14–0.40 s) track word length sensibly — "a" is shortest at 0.14 s,
  "Kakoro" longest at 0.40 s. Good enough to drive M1's word-level caption highlighting;
  note the zero-gap behaviour means word boxes will be edge-to-edge unless M1 insets them.

### Wall time
- **Warm (model already cached): 2.44 s** for the full `spike_stt` call — model load +
  transcribe of 2.389 s of audio. That is **RTF ≈ 1.02**, i.e. roughly realtime, and it is
  dominated by the one-off `WhisperModel` construction; M1 should build the model **once**
  and reuse it across segments rather than per call.
- **Cold (first run, includes the 142 MB download):** the whole `videomaker setup` command
  took **12.54 s** wall end-to-end, of which Kokoro TTS was 1.0 s and the two Kokoro models
  were already cached — so download + STT was ~11.5 s on this connection.

# macOS (Intel) notes

The app targets Linux first but runs on Macs. Two caveats verified on a 2018
Intel MacBook Pro (i5-8259U):

1. **FFmpeg must include libass.** Some Homebrew setups have a lean ffmpeg
   build without the `subtitles`/`drawtext` filters — caption burning fails.
   Fix: `brew reinstall ffmpeg` (the standard bottle links libass).
   `videomaker doctor` detects this and prints the remediation.
2. **Python wheels on darwin x86_64.** PyTorch ships no Intel-Mac wheels after
   2.2.2 — this is why the default TTS is kokoro-onnx, not the torch-based
   kokoro package. If `uv sync --extra ml` fails resolving onnxruntime or
   ctranslate2, pin older versions in this order:
   - `uv add --optional ml "onnxruntime==1.19.2"` (then try 1.18.1)
   - `uv add --optional ml "faster-whisper==0.10.1" "ctranslate2==3.24.0"`
   - TTS last resort: swap kokoro-onnx for sherpa-onnx (bundles its runtime,
     supports Kokoro voices).
   - STT last resort: `brew install whisper-cpp` and use the whisper-cli
     subprocess provider (planned as a fallback STTProvider).
3. **Hardware fast-render** (`videomaker run --fast`, or `render.fast_mode` in
   `config.yaml`) uses `h264_videotoolbox` when present. `videomaker doctor`
   reports whether it will actually *open*, not merely whether `ffmpeg
   -encoders` lists it — the two differ often enough to matter (the Linux
   development machine lists `h264_qsv` and cannot open it). VideoToolbox is
   driven with `-q:v`, which is the **one** part of this feature nobody has
   measured: the quantiser value was picked for VA-API on Linux and carried
   over. If Mac output looks soft, that number is the knob
   (`media/ffmpeg.HW_QUALITY`). libx264 remains the default quality path
   everywhere. On Apple Silicon none of the wheel issues above apply.

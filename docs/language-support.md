# Language support — what was measured, and what ships

The write-up behind M3 Task 21. Everything below was measured on
2026-08-31 on the primary target (Linux x86_64, this repo's `ml` extra, Kokoro
v1.0, faster-whisper `base`, FFmpeg with libass). It is a record, not a
capability table copied from a model card — the interesting results are the ones
that contradict the plan.

**Offered today: English (US and UK), Spanish, French, Italian, Portuguese.**
**Held back: Hindi, Japanese, Chinese.** Kokoro has voices for all nine.

## 1. The plan's premise was wrong, and the correction matters

The M3 plan predicted the **caption font** would be the binding constraint:
`CaptionStyle.font_name` is `DejaVu Sans`, DejaVu covers no Han and no
Devanagari, so Chinese captions should burn in as tofu boxes.

Two measurements say otherwise.

**DejaVu's own coverage — confirmed.** Parsing the `cmap` table of
`/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf` directly (5918 mapped
codepoints) and asking it for each language's sample text:

| language | DejaVu Sans |
|---|---|
| en, es, fr, it, pt | complete |
| hi | missing `इकटडतमरलवसहािीेैॉ्` |
| ja | missing `、すでのみイスソッテトドブラリー仕日本組語` |
| zh | missing `中作原固字工幕态文理的盘硬，` |

**But libass does not stop at the styled font.** An `.ass` whose style line says
`Fontname: DejaVu Sans`, burned through the real `subtitles` filter on a frame
carrying Spanish, Hindi, Japanese and Chinese, drew **all four correctly**.
libass asks fontconfig for a font covering each glyph the styled face lacks, and
this machine has Noto Sans CJK and Noto Sans Devanagari installed. There was no
tofu, and no warning, because there was nothing to warn about.

So the question the coverage gate must ask is libass's question —

> is there **any** installed font that can draw this character?

— which is what `media/fonts.py` asks, via the union of every installed font's
charset (`fc-list --format='%{charset}'`, one subprocess, ~140 ms, memoised).
Asking DejaVu alone would have blocked four languages that render fine, and
asking a hard-coded table would have blocked them on a machine where they work
and passed them on a machine where they do not.

## 2. What actually breaks: the round trip

The pipeline's caption timing comes from transcribing the narration it just
synthesised (`align`), so the honest test is a round trip: speak one sentence
with the language's own Kokoro voice, transcribe it back with the same
faster-whisper model the `align` stage uses, and compare.

**The first run of this was invalid and it is worth recording why.** `align`
passes the script as whisper's `initial_prompt`, and with a short clip whisper
will happily echo that prompt back. Chinese came back character-perfect —
because it was reading the prompt, not the audio. Every number below is from the
re-run with `hint_text=""`.

| lang | said | heard back (no prompt) | verdict |
|---|---|---|---|
| en | A solid state drive stores data in flash memory cells. | A solid state drives stores data in flash memory cells. | **good** |
| es | Una unidad de estado sólido guarda los datos en celdas de memoria flash. | (identical) | **good** |
| fr | Un disque à état solide stocke les données dans des cellules de mémoire flash. | En disque, à état solide stock, les données dans des cellules de mémoire et une flâche fre. | **good enough** — tail hallucination only; `snap_to_script` owns spelling |
| it | Un'unità a stato solido memorizza i dati in celle di memoria flash. | Un 'unità ha stato solido memorizzati in celle di memoria flash. | **good enough** |
| pt | Uma unidade de estado sólido guarda os dados em células de memória flash. | (identical) | **good** |
| hi | सॉलिड स्टेट ड्राइव डेटा को फ्लैश मेमोरी में रखती है। | `Solid state drive data کو plashmemory میں رکتی ہے.` | **broken** |
| ja | ソリッドステートドライブはフラッシュメモリにデータを保存します。 | そう言うとえんザープにギャレトすぎて… | **broken** |
| zh | 固态硬盘把数据保存在闪存单元中。 | 固太英坎巴士居暴存在三存單元寵物 | **broken** |

Three separate failures, none of them a font:

* **Japanese — the TTS.** Kokoro's `j*` voices were trained on `misaki[ja]`
  phonemes; kokoro-onnx phonemises through espeak-ng instead. espeak emits
  language-switch markers (`(en)dʒˈapəniːz(ja)`) that survive Kokoro's vocabulary
  filter, so the take literally says the English word "Japanese" — and runs
  17.3 s for a sentence that should take about 4 s.
* **Chinese — the TTS, subtly.** espeak's Mandarin phonemes lose tone once
  filtered against Kokoro's vocabulary. The audio sounds like Mandarin and is
  wrong, which is worse than obviously broken.
* **Hindi — the STT.** The audio is intelligible; faster-whisper `base` returned
  it transliterated into Urdu script even with `language="hi"` forced. Caption
  timings cannot be snapped back onto a Devanagari script from that.

Whisper `base` is weaker outside English and the plan said not to claim
otherwise. This is the claim, with its evidence: **five measured, four held
back, and the note on each held-back language in `videomaker/languages.py` says
which link broke.**

## 3. The font decision: ship nothing new

Task 21 asks for a deliberate call on bundling Noto Sans CJK and Noto Sans
Devanagari. **Decision: bundle neither.** `NOTICE.md` gains no font entry,
because no font was added.

1. **It would unblock nothing.** The measured blocker for all three held-back
   languages is the TTS or the STT, not the glyphs. Shipping a CJK font would
   change a video with wrong audio into a video with wrong audio and prettier
   captions.
2. **libass already falls back.** Where the OS has the fonts, captions render
   today with nothing bundled at all (§1). Bundling would duplicate what the
   platform provides, and would still need `fontsdir` plumbing into the
   `subtitles` filter to be reachable.
3. **The size is not incidental.** This repo ships ~933 KB of fonts for the UI.
   Noto Sans CJK is ~16 MB as a single-language OTF and over 100 MB as the OTC —
   a 17× to 128× increase in wheel size, for zero working languages.
4. **The remediation is one command, and `doctor` now names it.** On a machine
   that genuinely lacks the fonts, `videomaker doctor` reports which script
   cannot be drawn and how to install it — the same shape as the libass and
   ffprobe probes.

If Hindi, Japanese or Chinese are ever wanted, the work is `misaki[ja]` /
`misaki[zh]` G2P and a larger Whisper model — at which point the font question
can be revisited on its own merits, against a stack that would actually use it.

## 4. What the code does with this

* `videomaker/languages.py` — the registry: ISO code (whisper), espeak code
  (Kokoro), script sample (the font probe), and the verdict with its reason.
* `videomaker/media/fonts.py` — the live coverage probe, and the two gates
  combined in `offerable_codes()`.
* `web/voices.py` — the create form's menu, filtered to what clears both gates;
  `refusal_for()` is the same policy said in words, for a voice posted by hand.
* `doctor.py` — `caption font` (is the styled family actually installed, or was
  it silently substituted?) and `caption script coverage` (can anything draw the
  offered languages?).
* `ProjectStore.create` — derives `Project.language` from the voice's Kokoro
  prefix. This is why there is no separate language input to disagree with it,
  on the CLI or in the web form.

## 5. The verified caption frame

Spanish, written by `media/ass.py`'s own `write_ass` in the wide style and burned
through the real `subtitles` filter at 1920×1080:

> **estado sólido? Señal, año, corazón.**

`ó`, `ñ`, `á` and the inverted `¿` all draw, outlined, no `.notdef` boxes. The
accented glyphs are the cheap check for Latin-script languages and they pass.

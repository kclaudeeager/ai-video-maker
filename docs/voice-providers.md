# Voice providers over HTTP

Kokoro is the default narrator and it is free, local and offline. It also speaks
exactly the languages espeak-ng can phonemise, and `docs/language-support.md`
measured five of those as usable end to end. Swahili is reachable that way in
principle; **Kinyarwanda is not** — espeak-ng ships no voice for it, and Kokoro
phonemises through espeak-ng (`docs/multimodal-reader-design.md` §9.1).

A vendor's HTTP API reaches it. This document is how to point the reader at one
**without writing any Python**, and what that costs.

## A metered vendor spends real money

Read this part even if you skip the rest.

A whole Protestant Bible is about 790,000 words — roughly **5,270 minutes** of
narration at 150 words a minute. At a typical $0.10 per minute that is about
**$527**, and a bulk command could ask for it in one go.

So every paid synthesis run is estimated before the first request, and refused
past the budget unless it was confirmed:

| limit | default | how to go past it |
|---|---|---|
| minutes per run | 30 | pass `--yes` on the CLI, or press the confirm button in the reader, which shows the estimate |
| dollars per run | $1.00 | same |

The refusal names the estimate, the limit and the confirmation. A provider whose
`cost_per_minute_usd` is `0.0` is never blocked by the dollar limit — its cost is
zero and zero exceeds nothing — but the minutes limit still applies, because
thirty minutes of anything is worth a question.

This guard is `TTSBudget` in `providers/tts/http_api.py` and it is what stands
between a chapter and a bill. It is tested, including that a refused run makes
**no** HTTP call.

## Describing a vendor

Add a `voice_providers:` list to `config.yaml` (the shipped `config.example.yaml`
carries a commented example) and put the key in `.env` under the variable you
name. Then list the provider's `name` under `providers: tts:` and it is used like
any other voice.

```yaml
providers:
  tts: [vendor, kokoro]        # try the vendor first, fall back to Kokoro

voice_providers:
  - name: vendor
    endpoint: https://api.vendor.example/v1/tts
    api_key_env: VOICE_API_KEY
    auth_header: Authorization
    auth_format: "Bearer {key}"
    text_field: text
    voice_field: voice
    language_field: language
    speed_field: ""              # "" when the vendor takes no speed
    extra_body: {format: mp3}    # sent verbatim with every request
    audio_response: raw          # the reply body is the audio
    voices: [nyira, mugabo]
    languages: [rw, sw]
    cost_per_minute_usd: 0.10
    requests_per_minute: 60
    requests_per_day: 1000
```

```
# .env
VOICE_API_KEY=...
```

Every field and its default is the `HTTPTTSConfig` model in
`providers/tts/http_api.py`; the model is the reference, this page is the tour.

- **`languages`** is what decides which works offer *Listen* in the reader. A work
  whose language no configured voice lists can still be read and briefed, just not
  heard; the mode is absent rather than present and broken.
- **`audio_response: base64_json`** with `audio_json_path: data.audio` is for
  vendors that wrap the audio in JSON. The path is dotted, and a number selects a
  list element (`data.audio.0.content`).
- **The key never appears in a log line or an error.** It is read from the
  environment at request time and redacted from every message the provider
  raises; a test asserts it.

No vendor name appears anywhere in the code. If you find yourself wanting to add
one, add a YAML entry instead.

## Rate limits, and why they are a separate concern from the budget

The budget stops you spending too much. The rate limiter stops you being cut off
mid-chapter — a different failure with a different fix. A chapter is dozens of
short requests back to back, which is exactly the shape a per-minute cap rejects,
and a bulk run is thousands.

- `requests_per_minute` is paced **client-side, by sleeping** — evenly, one
  request every `60 / n` seconds. Being rate-limited is a bug in the caller, not
  an event to handle.
- `requests_per_day` is counted in the same ledger as every other provider's
  quota (`~/.cache/ai-video-maker/quota.json`), so it survives a restart and is
  shared between the CLI and the web worker. A run that would cross it stops
  **before the first request**, with the count and the reset time (00:00 UTC),
  rather than failing at verse 300.
- `429` and `5xx` are retried with exponential backoff and full jitter, honouring
  `Retry-After`, up to `retry_attempts`; then the run stops with `RateLimited`.
  Any other `4xx` is **not** retried — a malformed request retried four times is
  four times wrong.
- `max_concurrency` defaults to `1`. Verse-by-verse synthesis is naturally
  serial and a reader is waiting; parallelism buys little here and is the fastest
  way to trip a cap.
- Zero means "no stated limit" and applies no throttle.

**Resume, do not restart.** The reader caches every verse's audio as it arrives,
so a run stopped by a rate limit or by the budget keeps every verse already made
and, run again, synthesises only the missing ones. That property is what makes a
daily cap survivable rather than fatal.

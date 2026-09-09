# Multimodal reader — one work, four ways to take it in

The design behind M7. The owner's brief: *"help busy or lazy readers understand
the book, by turning the book into text / audio / visual on the reader's
preference — starting with the Bible, and supporting multiple languages as they
become available."*

This document is the *why*. The task list is
`docs/superpowers/plans/2026-09-09-m7-library-and-reader.md`.

It builds on `docs/source-driven-video-design.md`, which already argued for
bring-your-own-text (Idea 1) and the LLM wiki (Idea 2). Nothing there is
retracted. What is added here is the piece that document did not have: **the
reader is the product, and the video is its most expensive mode, not its only
one.**

---

## 1. The one decision everything else follows from

The obvious reading of the brief is "a Bible-to-video generator". That reading
is wrong, and expensively so.

A finished 2-minute illustrated video is the *most* expensive artefact this
codebase can produce and the *least* useful one to a busy reader, who wants to
know what Job 38 is about while walking to a bus. The cheap artefacts — the text
itself, a plain-language brief, a narration track — cost approximately nothing
and serve that reader better.

So the modes are ranked by cost, and **each mode is a prefix of the next**:

| mode | what the reader gets | what runs | marginal cost |
|---|---|---|---|
| `source` | the passage itself, versified, in their language | nothing | **zero** |
| `brief` | a plain-language summary, who/where/what-changes | one LLM call | ~1 call |
| `listen` | a narration track of the source or the brief | `script → voice → align` | CPU only |
| `watch` | the illustrated cut, wide + Short | the whole chain | images + CPU minutes |

The prefix property is not a coincidence to be admired; it is the design
constraint. **Upgrading a mode must never redo work already paid for.** A reader
who read the brief, then listened, then watched must have spent one LLM call, one
synthesis and one render — not three of each. That is precisely what
`cache/stages.json` already guarantees for the video pipeline, and the reader
inherits it by being built out of the same stages rather than beside them.

It also fixes the cost problem the existing design doc flagged and left open:

> A 10-episode documentary series could be the first thing in this project that
> genuinely costs money.

It still could. But it is now the *fourth* thing a reader can ask for, opted into
one passage at a time, rather than the only door into the product.

---

## 2. What is added to the codebase, and what is deliberately not

### 2.1 A library layer, above projects

A `Project` is video-shaped: it has a topic, a template, scenes, and two
`OutputSpec`s. It is the wrong container for "the Gospel of John". So M7 adds
one layer above it, and the layer owns the *text*:

```
<workspace>/library/<work-slug>/
├── work.yaml            # title, language, licence, versification, provenance URL
├── source/              # immutable as ingested: USFM/USJ exactly as downloaded
├── units/               # normalised, addressable: units/JHN/003.json
├── derived/             # briefs and outlines, cached by content hash
└── wiki/                # M7 Phase D: index.md, log.md, entities/, chapters/
```

`source/` is never edited, only replaced by a re-import. `units/` is derived from
it deterministically. `derived/` is derived from `units/` by an LLM and is
therefore cache-keyed and disposable. That is the same three-way split the
project folder already makes between `project.json`, `build/` and `cache/`, and
for the same reason: **the only thing that must survive a bug is the input.**

### 2.2 The Bible is a corpus provider, not a special case

The brief says "start with the Bible" and "the book" in the same sentence. If
scripture is special-cased into the pipeline, the second half never happens. So
the Bible enters through the existing provider abstraction as a new kind:

```python
class CorpusProvider(ABC):
    def works(self) -> list[WorkRef]: ...
    def outline(self, work_id: str) -> list[UnitRef]: ...
    def unit(self, ref: UnitRef) -> UnitText: ...
```

`BibleCorpus` implements it over the bundled public-domain texts. A future
`EpubCorpus` implements it over any public-domain book the reader uploads, and
the reader UI, the brief, the audiobook and the video path do not know the
difference. The provider chain machinery (`Settings.provider_chains`,
`provider_override`, `--providers mock`) already exists and is reused unchanged.

`UnitRef` carries a `versification` field from the first commit. Psalm numbering
and Malachi's chapter count differ between the Masoretic, Vulgate and LXX
traditions, and a schema that assumes KJV numbering is universal will be wrong
the first time a non-English text is added. Book identifiers are **USFM/Paratext
three-letter codes** (`GEN`, `JHN`, `REV`) — the identifiers every USFM tool
already speaks — not OSIS, not invented ones.

### 2.3 What is deliberately not added

- **No new stage in `STAGE_ORDER`.** `cache.STAGE_ORDER`'s own comment records
  why `thumbnail` was appended last: `derive_status` returns the status of the
  last *current* stage, so appending cannot lower any project's status.
  *Prepending* a `digest` stage would do the opposite — every project on disk
  would find no cached hash for it, go stale, and drop to `new`. The brief is
  therefore produced in the library layer and handed to the project as source
  text, not computed inside the pipeline.
- **No new stage for the audiobook either.** `preview.py` is the precedent: an
  artefact built on demand, keyed `preview:<aspect>` in the same `StageCache` as
  the stages yet deliberately outside `STAGE_ORDER`. `audiobook.py` is built the
  same way and cached at
  `audiobook:<lang>`, buildable the moment `align` is current. That is what lets
  `listen` stop before the storyboard gate without inventing a fourth gate or a
  seventh `Status`.
- **No vector store, no embeddings.** For the same reasons the existing design
  doc gives, and one more: PyTorch remains banned by this project's constraints.
- **No new database.** Plain files, atomic writes, content hashes. As before.

---

## 3. Fidelity: the part that is actually hard

The script stage today *invents*. Its docstring and its `_OUTPUT_CONTRACT` are
built around asking a model to write narration from a topic. For scripture that
behaviour is not a feature to be tuned down; it is a defect.

A reader who asks to hear John 3 and is read a language model's paraphrase of
John 3, with no marker saying so, has been misled. It does not matter that the
paraphrase is good. **The failure mode is silent, and it is the single most
damaging thing this product can do**, because the reader has no way to detect it
and every reason to trust it.

So fidelity becomes an explicit, three-valued property of the template:

| `source_fidelity` | narration is | who writes it |
|---|---|---|
| `invent` | written from a topic | the LLM (today's behaviour, and the default) |
| `adapt` | the source, restructured for the ear | the LLM, diffed at gate 1 |
| `verbatim` | **byte-identical to the source text** | a deterministic segmenter |

`verbatim` is the important one, and it is also the cheapest. Under `verbatim`
the LLM is **never asked for narration at all**. A pure function splits the
passage on verse and sentence boundaries into a word budget, and the LLM is asked
only for the thing it is actually good at here — `visual_queries` per scene. Three
things fall out of that:

1. The narration can be asserted equal to the source in a unit test. Drift
   becomes a failing build rather than a matter of taste.
2. `script:all`'s fingerprint covers the source text, so re-importing a
   translation re-segments while leaving voiced takes for unchanged units intact.
3. The quota cost of scripture narration drops to zero LLM tokens for the words
   themselves.

Every scene gains `source_ref: str = ""` (new field, empty default, so every
`project.json` on disk loads unchanged — the same convention `folder`,
`thumbnail_text` and `alt_queries` already follow). Gate 1 shows it. The reader UI
shows it. `adapt` and `brief` output are **labelled in the interface as a
retelling**, next to a one-tap path to the verses behind it. That label is not a
disclaimer bolted on at the end; it is the feature that makes the retelling
trustworthy enough to be worth having.

---

## 4. Pictures of scripture: the risk nobody schedules time for

Two problems, one of them technical.

**The technical one.** M1 measured stock relevance at 6/10 for a *tech
explainer*, where Pexels actually has footage. Pexels has nothing for first-century
Judea. AI images become the primary source rather than the fallback, which inverts
the economics M1 measured — a 2-minute video cost **zero** Cloudflare neurons
because stock covered it. Scripture video will not.

**Nobody has measured what it costs.** Before Phase C builds anything, one task
does nothing but render a single chapter with `visual_kind_order: [ai_image]` and
report neurons spent per finished minute against the free daily allowance. If that
number says a chapter costs a meaningful share of a day's quota, the honest answer
is that `watch` is a per-passage treat and the product's centre of gravity is
`brief` and `listen` — which is what §1 already argues, and which would then be
measured rather than asserted.

**The other one is editorial, and it does not have a technical fix.** Depicting
holy figures is contested — welcomed in some traditions, forbidden in others — and
AI-generated faces of religious figures is the single most reputationally
dangerous artefact this codebase could emit. It would also break the existing
`_OUTPUT_CONTRACT`, which already ends *"no named people or brands"*.

The default is therefore **non-figurative**: landscape, place, architecture,
artefact, natural phenomenon, era-appropriate object. A wilderness at dusk for the
temptation; a millstone for the millstone. Two supports make it real rather than
aspirational:

- `visuals.depict_figures: bool = False` in `Settings`, defaulted off, documented
  as the reader's editorial decision rather than the tool's theology.
- A negative prompt and a query contract in the scripture template that name the
  failure explicitly, in the style `script.py` already uses — the module's own
  comment records that *"an instruction the model can satisfy while being wrong
  needs a counter-example, not a firmer adjective."*

Worth investigating in Phase C and not before: **openbible.info's Bible geocoding
data (CC BY)** plus public-domain Holy Land photography would give the place-name
scenes real photographs of real places, which is both cheaper than generation and
more honest than an invented landscape.

---

## 5. Which texts ship, and under what licence

Researched 2026-09-09. The project's existing rule for music — *ship nothing whose
licence you cannot state in one sentence* — applies here unchanged, and it decides
this table.

| text | language | licence | format at source | bundle? |
|---|---|---|---|---|
| **World English Bible (WEB)** | en | **public domain** | USFM (ebible.org) | **yes — default** |
| **Berean Standard Bible (BSB)** | en | **CC0** since 2023-04-30 | USFM, USX, **USJ (JSON)** (berean.bible) | **yes — modern readability** |
| KJV | en | PD in the US and internationally; UK *printing* sits under perpetual Crown letters patent held by CUP | USFM (ebible.org) | later; carry the note |
| ASV 1901, Darby, YLT, Douay-Rheims | en | PD | USFM (ebible.org) | later, cheap |
| Reina-Valera **1909** | es | PD | USFM (ebible.org) | next, after English |
| Reina-Valera 1960 | es | **copyright, UBS** | — | **never** |
| Louis Segond 1910 | fr | PD | USFM (ebible.org) | next, after English |
| Almeida (historic editions) | pt | PD *for the historic edition only*; modern revisions are not | text | verify the exact edition first |
| Swahili, Kinyarwanda | sw, rw | **unverified** | USFM via ebible | **do not bundle until checked** |
| unfoldingWord ULT/UST | en | CC BY-SA 4.0 | USFM (git.door43.org) | optional; attribution + share-alike on the files |

Two rules this table encodes:

- **ebible.org has no site-wide licence.** Its own legal page says each translation
  is individually licensed and the "about" page is the authority. So the importer
  reads the per-translation licence metadata (the `BibleNLP/ebible` corpus records
  it per language) and **refuses to import a text whose licence it cannot read**.
  That refusal is a feature, and it is what makes the Kinyarwanda and Swahili rows
  safe to leave unresolved rather than guessed.
- **API.Bible cannot be bundled.** Its terms permit caching only with a 30-day
  refresh and explicitly forbid redistributing API content without written
  permission. It may exist later as a *live* provider behind `CorpusProvider`; its
  output must never land in `source/`.

`work.yaml` carries `licence`, `licence_url` and `source_url` for every imported
work, and the reader UI shows them. `NOTICE.md` gains a section listing the
bundled texts. Neither is optional.

**Ship WEB and BSB.** One classic register, one modern; both zero-restriction;
BSB ships JSON at source, which removes a conversion step for the first import.

---

## 6. Multiple languages, honestly

`docs/language-support.md` already did this work and its conclusion binds here:
**English, Spanish, French, Italian and Portuguese are offered; Hindi, Japanese
and Chinese are held back**, because the TTS phonemiser and the STT model break
on them, not because of fonts.

A language is only usable end to end when four things line up:

1. a redistributable text in that language (§5),
2. a Kokoro voice whose round trip survived (`languages.py`),
3. an installed font libass can fall back to (already handled by `media/fonts.py`),
4. the reader's own preference.

English clears all four today. Spanish and French clear 2–4 and need only the text,
which is why RV1909 and LSG are the next two imports and why they are cheap.

**Kinyarwanda does not clear (2), and this should be stated plainly rather than
promised.** Kokoro has no Kinyarwanda voice, and faster-whisper alignment for it is
weak — which means captions timed from the narration would be wrong, which is the
exact failure `languages.py` exists to prevent. Reaching it needs a different TTS
provider behind `TTSProvider`, and that is its own milestone, not a line item here.
The `source` and `brief` modes have no such dependency: **a language with a text
but no voice can still be read, just not heard.** M7's mode table degrades
per-language rather than blocking, and the UI says which modes a language supports
instead of failing at synthesis time.

---

## 7. Scale, in numbers

The Protestant canon is 66 books, 1,189 chapters, ~31,100 verses, roughly 790,000
English words. The New Testament is 260 chapters, ~7,950 verses, ~180,000 words.

| mode, whole Bible | derived quantity | verdict |
|---|---|---|
| `source` | ~5 MB of text | trivial; bundle it |
| `brief` | 1,189 LLM calls | one-off; fits Gemini Flash's 1,500/day. Bulk-precomputable overnight |
| `listen` | ~87 hours of audio at 150 wpm (NT alone: ~20 h) | CPU time, not money. **Measure Kokoro's realtime factor before promising a bulk build** |
| `watch` | 1,189 chapters × N generated images | **infeasible in bulk, and it should not be attempted** |

That table is the product strategy stated as arithmetic. `source` and `brief` can
cover the whole book. `listen` can cover it given a machine and a night. `watch` is
per-passage, on demand, and human-gated — which is the same three-gate curation
argument the project was founded on, arriving from a different direction.

The quota split from the existing design doc still holds: **bulk work on Gemini
Flash** (1M TPM, 1,500 requests/day), **per-passage work on Groq** (fast, small
payloads; its 6K TPM makes bulk painful).

---

## 8. What would make this design wrong

Recorded so the next person can check rather than re-derive:

- **If `brief` is not actually useful**, the whole ranking collapses and this is
  just a video tool with extra layers. Phase B ships `brief` first and alone for
  exactly this reason — it is the cheapest thing to build and the fastest thing to
  abandon.
- **If readers want the video and nothing else**, §1's ordering is inverted and
  the image-cost measurement in Phase C becomes the critical path rather than a
  footnote.
- **If verbatim segmentation reads badly aloud** — Kokoro handling archaic
  syntax, verse numbers spoken or swallowed, "Selah" — then `verbatim` needs a
  narration-normalisation pass, which is a fifth thing between source and speech
  and needs its own diff at gate 1. Listen to a chapter before building Phase C.
- **If the wiki layer (Phase D) drifts**, as the existing doc warns, it becomes
  maintenance rather than leverage. It is last in the plan and it is severable.

---

## 9. Decisions since the first draft

Recorded here rather than in a commit message, because each one changed the
plan and the next person will otherwise re-derive it.

### 9.1 The phonemiser, not the model, is what blocks Kinyarwanda

`docs/language-support.md` found that the binding constraint on a language is
the TTS phonemiser rather than the caption font. That finding extends further
than the nine Kokoro languages. Measured on 2026-09-09:

```console
$ espeak-ng --voices | wc -l
132                              # 131 voices plus the header
$ espeak-ng --voices | grep -iE ' (kin|rw) '
                                 # nothing
$ espeak-ng --voices | grep -i swahili
 5  sw     --/M  Swahili
```

espeak-ng ships **Swahili and not Kinyarwanda**. Kokoro phonemises through
espeak-ng, and so does Piper by default. So no amount of local training reaches
Kinyarwanda by that route: it needs character-level training against a regular
orthography, a Kinyarwanda voice contributed upstream to espeak-ng, or a vendor
API. That is a fact about the toolchain, not about the data.

**Consequence for sequencing: Swahili before Kinyarwanda.** Swahili clears every
gate — espeak support exists, and there is an openly licensed corpus (below) —
while Kinyarwanda needs a build or a vendor. Swahili is also the larger market.

### 9.2 Licence audit of the openly published African speech assets

Checked on Hugging Face, 2026-09-09. Re-check before relying on any of it: model
cards change, and a missing licence field is not the same as a permissive one.

| artefact | type | licence | commercial use |
|---|---|---|---|
| Afrivoice Kinyarwanda (~3,163 h) | corpus | CC-BY-4.0 | yes |
| Afrivoice Swahili (~3,097 h) | corpus | CC-BY-4.0 | yes |
| `kinyarwanda-tts-dataset` (~6 h, single voice) | corpus | CC-BY-4.0 | yes |
| Mbaza ASR Afrivoice 660 h | Kinyarwanda ASR | CC-BY-4.0 | yes |
| `stt_rw_sw_lg_conformer_ctc_large` | rw/sw/lg ASR | CC-BY-4.0 | yes |
| `mbaza_bert` | Kinyarwanda LM | MIT | yes |
| Meta MMS `mms-tts-kin` | Kinyarwanda TTS | **CC-BY-NC-4.0** | **no** |
| Meta MMS `mms-1b-all` | multilingual ASR | **CC-BY-NC-4.0** | **no** |
| XTTS-based Kinyarwanda voice | TTS | tagged MIT, fine-tuned from Coqui XTTS-v2 under the **non-commercial** Coqui Public Model Licence | **unsafe** |
| Parakeet 3,000 h Kinyarwanda fine-tune | ASR | **no licence stated** | **undefined** |
| Google WAXAL | corpus | CC-BY-4.0 (some subsets CC-BY-SA) | yes, but **contains no Kinyarwanda** |

Two things to carry forward. **A permissive tag on a fine-tune does not undo a
restrictive licence on its base** — the XTTS row is the trap, and it is the kind
that is discovered at the worst moment. And **a repository with no licence field
is not permissive**; it is undefined, which is worse, because there is nobody to
have got it wrong.

The shape of the conclusion is good news: for Kinyarwanda the *data* is open and
commercially usable and the *voice* is not, so the gap is a bounded engineering
project rather than a licensing dead end.

### 9.3 Voices are a vendor-neutral configuration, not a dependency

Given 9.1 and 9.2, the reader must be able to reach a hosted voice API for
languages the local stack cannot phonemise — and must not acquire a vendor in
the process.

So the MVP adds one generic `HTTPTTSProvider` configured entirely from YAML:
endpoint, auth header, request-field mapping, response shape, supported
languages, cost per minute, rate limits. **No vendor name appears anywhere
except a configuration file**, and adding one is YAML rather than Python. The
existing `TTSProvider` ABC and `Settings.provider_chains` already carry the
fallback semantics; nothing about the architecture changes.

Two guards ship with it, and both are requirements rather than polish:

- **A budget guard.** A whole Bible is ~5,270 narration minutes. At $0.10 a
  minute that is ~$527 in one command. Estimate before synthesising, refuse
  above a limit unless explicitly confirmed.
- **A rate limiter.** A chapter is dozens of short requests back to back, which
  is the exact shape a per-minute cap rejects; a bulk run is thousands. Pace
  client-side, track the daily allowance in the existing `QuotaTracker` ledger
  so it survives a restart, retry `429`/`5xx` with jittered backoff honouring
  `Retry-After`, and never retry any other `4xx`.

The property that makes both survivable is **per-verse caching**: an
interrupted run resumes from the first missing verse instead of starting over.

### 9.4 The reader synthesises directly; it does not build a `Project`

The first draft had `listen` materialise a `Project` and run `script → voice →
align`. That has been dropped for the MVP.

A `Project` is video-shaped, and reaching it means touching the stage
fingerprints, the gates and `STAGE_ORDER` — the three places in this codebase
where a mistake destroys work that already exists. The reader needs none of it:
it segments the passage by verse, synthesises each verse, concatenates, and
writes a `.vtt` from the per-verse durations.

Three things fall out, all of them wins:

1. **Fidelity is true by construction.** Narration is the source text because
   no model was ever asked to write it. A test asserts the reassembled
   narration equals the source.
2. **No forced alignment, so no STT in the MVP.** Verse-level timing comes free
   from synthesising verse by verse, and verse level is the right granularity
   for scripture anyway. `faster-whisper` is not an MVP dependency.
3. **The video pipeline cannot regress**, because nothing in it is touched.

Video mode is what materialises a `Project`, later, and it inherits all three
gates when it does.

### 9.5 The reader is a reading surface, not another dashboard

`docs/ui-design.md` already decided the direction — achromatic chrome, the only
hue on a page being the one asking for a human, links underlined rather than
coloured, every value a token. The reader applies it rather than opening a
second direction: a measured column of serif text, verse numbers as quiet
unselectable superscripts, a three-way mode switcher marked by weight rather
than colour, and the brief in a sunk card under a warm eyebrow reading *a
retelling*. That warm eyebrow is the one hue on the page, which is exactly what
the palette rule reserves warmth for.

### 9.6 Still open

- The per-minute cost of the local pipeline is **still an estimate**. Measure it.
- Whether a brief is actually useful is the assumption the whole mode ranking
  rests on. Ship it first, watch which mode readers choose.
- Whether a synthetic voice is acceptable to organisations that record human
  community voices deliberately. Ask three of them before building for them.

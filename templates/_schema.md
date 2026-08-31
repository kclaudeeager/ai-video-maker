# Niche template schema

A **niche template** is the editorial recipe for one kind of video: the voice the
script is written in, the beats it moves through, how long it runs, and what kind
of footage illustrates it.

Templates are plain YAML files in this directory. **Adding one requires no Python.**
Drop a file in, and `videomaker new "<topic>" -t <name>` can use it.

## File rules

- One template per file, named `<name>.yaml` (lowercase, `snake_case`, `.yaml` — not `.yml`).
- The filename stem **is** the template name, and the `name:` field inside must match it.
- Files beginning with `_` (like this one) are documentation and are never loaded.
- Unknown fields are rejected. A typo is an error, not a silently ignored key.

Loading is `videomaker.templates.load_template(name)`; `list_templates()` returns
every name in this directory. Both raise `ValueError` with the list of available
names when a template is missing, malformed, or fails validation.

## Fields

| Field | Type | Required | Default | Meaning |
| --- | --- | --- | --- | --- |
| `name` | string | yes | — | Machine name. Must equal the filename stem. This is what `-t` takes. |
| `display_name` | string | yes | — | Human-readable label for the CLI and the M2 web UI. |
| `system_prompt` | string | yes | — | The system prompt handed to the LLM by the script stage. This is the template. See below. |
| `structure` | list of strings | yes | — | Named beats, in order, at least one. The script stage walks them to shape the scene list. |
| `short_beats` | list of strings | no | `[]` | Which of those beats the vertical Short is cut from. Empty means "no opinion": every scene stays in the Short, as before this field existed. Every entry must appear in `structure`. |
| `words_per_minute` | integer > 0 | no | `150` | Assumed narration pace. Used to turn `--minutes` into a scene count and a word budget. |
| `scene_count` | `[min, max]` integers | yes | — | Hard bounds on how many scenes a video may have. `1 <= min <= max`. |
| `visual_kind_order` | list of visual kinds | yes | — | Preference order the visuals stage tries when a scene's kind is `auto`. At least one entry. |
| `caption_style` | string | no | `"default"` | Named caption look (font, size, position). M1 ships `default` only. |
| `music_mood` | string | no | `"calm"` | Music bed mood — the subdirectory of `assets/music/` a track is picked from. |
| `sfx_profile` | string | no | `"subtle"` | How loud this template's sound effects sit: `subtle`, `punchy` or `none`. |

### `visual_kind_order`

Valid entries, from `videomaker.models.VisualKind`:

- `stock_video` — stock footage clip (Pexels)
- `stock_photo` — stock still, given motion by a pan/zoom
- `ai_image` — generated still (Cloudflare Workers AI, Flux schnell)
- `auto` — meaningless here; do not use it in a template

**Put stock kinds first.** The visuals stage tries each kind in order and stops at
the first that returns something usable, so `[stock_video, stock_photo, ai_image]`
keeps generated-image spend near zero and only reaches for the model on scenes the
stock libraries cannot cover.

### `scene_count` and duration

The script stage asks for `target_scene_count(minutes)` scenes:

```
round(minutes * words_per_minute / 30)   clamped into [min, max]
```

30 is the assumed average words per scene (`videomaker.templates.AVG_WORDS_PER_SCENE`).
The clamp is absolute: a 10-minute request against `scene_count: [4, 10]` still yields
10 scenes — longer scenes, not more of them. Pick bounds you would be happy to watch at
both ends of the range, and write the prompt so each scene can breathe at the wide end.

### `short_beats` and the Short

Every project renders twice: a wide cut of all the scenes, and a vertical Short
built from the scenes ticked `in_short`. `short_beats` decides which scenes start
out ticked.

**Do not leave it at every beat.** A Short that is the whole video re-cropped is
exactly the low-effort repurposing the platforms suppress, and it is where the
numbers are worst: engagement peaks at 30–45 s on YouTube Shorts, 21–34 s on
TikTok and under 30 s on Reels, which does not recommend anything over three
minutes to new audiences at all. Three minutes (`assemble.MAX_SHORT_S`) is only
where the *upload* stops being accepted; `assemble.SHORT_TARGET_S` (45 s) is where
a Short is actually competitive, and the storyboard nudges toward it without ever
blocking on it.

For a five-beat explainer, `[hook, mechanism, close]` — the opening, the heart and
the landing — is the recommendation (`templates.RECOMMENDED_SHORT_BEATS`). Pick
beats that survive on their own, and say so in `system_prompt`: a scene bound for
the Short is played without its neighbours, so it must not open with "and" or lean
on something an earlier scene established.

Editing `short_beats` deliberately does **not** stale anything. It is excluded from
`Template.script_fingerprint`, so retuning which beats make the Short cannot rewrite
the narration of projects already written — and adding the field in the first place
did not restage a single one. The consequence to know about: it applies when the
script is written, so an existing project keeps the ticks it has. Change those on
the storyboard, where you can see the running time.

The selection is a **default**, not a rule. `script.assign_beats` maps the scenes
the model returned onto `structure` positionally — the first scene takes the first
beat, the last takes the last, the rest spread evenly between — so it is a good
guess and not a certainty. The storyboard shows the ticks and a person can change
any of them; once they do, the scene is pinned and no later run may move it back.

### `sfx_profile` and the sound effects

Effects come from **your own** `assets/sfx/`, which the project ships empty and
always will (`docs/audio-design.md`). So on a fresh clone every profile — `punchy`
included — places nothing at all, and that is not a misconfiguration: it is the
default state of the tool.

Once there are files in it, two things happen, both decided from information the
pipeline already has and neither costing an LLM call or a look at the footage:

- **A `transition/` effect plays on every scene cut.** `scene_timeline` already
  knows every cut timestamp. One reused file is the point — it is what makes a run
  of cuts read as deliberate rather than abrupt, and it is the highest return per
  unit of work in the whole audio design.
- **The beats in `structure` are marked.** `hook` and `close` take an `accent/`
  effect; `mechanism` is risen into with a `riser/`, starting a second early. The
  beats in between carry the argument and are deliberately left alone.

The three profiles are levels, not different sounds:

| profile | what it is for |
| --- | --- |
| `subtle` | the default. Present under the narration, never over it. |
| `punchy` | about 7 dB louder, for a template whose register can carry it. |
| `none` | no effects at any level, for a subject a whoosh would cheapen. |

`config.yaml` can switch the layer off globally (`audio.sfx_enabled`) or just the
cuts (`audio.transition_sfx_enabled`); the template decides the level, the config
decides whether it plays at all.

Like `short_beats`, `sfx_profile` is excluded from `Template.script_fingerprint`:
turning the effects down must not rewrite the narration of projects already
written, and adding the field must not have restaged a single one.

**Literal foley is out of scope**, and that is a decision rather than a gap. Stock
clips arrive effectively mute, so there is nothing to sync a key click against, and
a hit slightly out of time with what is on screen reads as worse than silence.

### Writing `system_prompt`

This is where the quality of the channel lives. The whole point of this tool is
**curated original content**, not templated spam, so the prompt should:

- Name the audience and the register ("a curious adult who is smart but not a specialist").
- Describe each beat in `structure` and what it must accomplish, in the same order.
- Demand original wording — never reproduce phrasing from a source.
- Forbid invention: say only what is confidently true; write around uncertain numbers,
  dates and names rather than fabricating them.
- Ban the house-style tells: filler openers, hype adjectives, rhetorical questions,
  engagement-bait closers, emoji, markdown, stage directions.
- Require each scene to add new information.
- State a per-scene word target consistent with `words_per_minute`
  (about 25–35 words per scene at 150 wpm gives 10–15 second scenes).
- Ask for a concrete, literal, filmable visual search query per scene — nouns and
  places, never abstractions like "innovation". Abstract queries return abstract
  stock, which is the fastest way to make a video look generic.
- Say that the `short_beats` scenes are also cut together on their own, so each of
  them has to stand up without the scenes around it.

Keep it a *system* prompt: it describes how to write, not what this particular video
is about. The topic, target duration and scene count are supplied separately by the
script stage.

## Minimal example

```yaml
name: my_template
display_name: My Template
system_prompt: |
  You are ...
structure: [hook, body, close]
short_beats: [hook, close]
scene_count: [4, 8]
visual_kind_order: [stock_video, stock_photo, ai_image]
```

## Checklist before opening a PR

- [ ] Filename stem, `name:` and the `-t` value you intend all match.
- [ ] `uv run python -c "from videomaker.templates import load_template; print(load_template('my_template').display_name)"` succeeds.
- [ ] `videomaker new "some topic" -t my_template` then `videomaker run <id>` produces a script you would publish.
- [ ] The prompt names the beats in `structure`, and the generated script actually hits them.
- [ ] `short_beats` is a *subset* of `structure`, and the scenes it selects read as a
      complete Short on their own — near 45 s, not the whole video re-cropped.
- [ ] Visual queries came back concrete enough that the stock search found real footage.
- [ ] `sfx_profile` is one of `subtle`, `punchy`, `none` — and you have listened to a
      render with your own `assets/sfx/` in place before settling on it.

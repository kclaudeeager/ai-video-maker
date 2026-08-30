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
| `words_per_minute` | integer > 0 | no | `150` | Assumed narration pace. Used to turn `--minutes` into a scene count and a word budget. |
| `scene_count` | `[min, max]` integers | yes | — | Hard bounds on how many scenes a video may have. `1 <= min <= max`. |
| `visual_kind_order` | list of visual kinds | yes | — | Preference order the visuals stage tries when a scene's kind is `auto`. At least one entry. |
| `caption_style` | string | no | `"default"` | Named caption look (font, size, position). M1 ships `default` only. |
| `music_mood` | string | no | `"calm"` | Music bed mood. Recorded now; used from M3. |

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
scene_count: [4, 8]
visual_kind_order: [stock_video, stock_photo, ai_image]
```

## Checklist before opening a PR

- [ ] Filename stem, `name:` and the `-t` value you intend all match.
- [ ] `uv run python -c "from videomaker.templates import load_template; print(load_template('my_template').display_name)"` succeeds.
- [ ] `videomaker new "some topic" -t my_template` then `videomaker run <id>` produces a script you would publish.
- [ ] The prompt names the beats in `structure`, and the generated script actually hits them.
- [ ] Visual queries came back concrete enough that the stock search found real footage.

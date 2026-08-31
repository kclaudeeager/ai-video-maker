"""The script stage: one LLM call turns a topic and a template into scenes.

The model is asked for JSON against an **inlined** schema — no `$ref`/`$defs`, which
several structured-output endpoints reject and which nothing downstream needs — and
the reply is validated with pydantic. A reply that does not validate earns exactly
one repair retry carrying the validation error back to the model; a provider that
fails that twice is abandoned for the next one in the chain, because a model that
cannot follow a schema twice will not follow it a third time.
"""

import json
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from videomaker.cache import hash_inputs, stage_key
from videomaker.models import Project, Scene, SceneVisual
from videomaker.pipeline.base import ADVANCE_ON, StageDeps, StageResult, call_chain
from videomaker.providers.base import LLMProvider
from videomaker.providers.errors import ProviderResponseError
from videomaker.templates import Template, load_template

STAGE = "script"
TEMPERATURE = 0.7
MAX_TOKENS = 4096
SCENE_ID_FORMAT = "s{:02d}"

#: A malformed reply is a *response* problem, so it also advances the chain.
_SCRIPT_ADVANCE_ON = (*ADVANCE_ON, ProviderResponseError)

#: How many stock searches one scene is given, written in one call at no extra cost.
#: Two is the floor because the ladder needs somewhere to go; three is the ceiling
#: because a fourth would be past `ranking.MAX_QUERY_ATTEMPTS` and never searched.
MIN_QUERIES_PER_SCENE = 2
MAX_QUERIES_PER_SCENE = 3

#: The output contract, few-shot examples and all.
#:
#: **This lives here and not in `tech_explainer.yaml`, deliberately.**
#: `Template.script_fingerprint()` is `model_dump_json` minus two fields, so a
#: single edited byte of a template's `system_prompt` stales `script:all` for every
#: project on disk — and `run_script` then replaces `project.scenes` **wholesale**,
#: taking every voiced take, chosen shot and approval built on them with it. M3
#: Task 22 walked into that trap once already. The output contract is in no
#: fingerprint at all: it is versioned with the code, which is the honest place for
#: it, because what makes a good query is a property of the *stock library* the
#: search hits, not of a template's editorial voice.
#:
#: The examples are not decoration. M1's prompt already said "concrete, literal,
#: filmable nouns" and the model still wrote "capacity sticker" for a scene about
#: NAND cells wearing out — Pexels answered with a warehouse shelf stencilled
#: "LOAD CAPACITY PER SHELF 200 KG". An instruction the model can satisfy while
#: being wrong needs a counter-example, not a firmer adjective.
_OUTPUT_CONTRACT = (
    "Reply with a single JSON object and nothing else: no prose, no markdown fence. "
    'It has one key, "scenes", an array of objects with exactly two keys: '
    '"narration" (what the narrator says, spoken prose only) and "visual_queries" '
    f"(an array of {MIN_QUERIES_PER_SCENE} to {MAX_QUERIES_PER_SCENE} stock-footage "
    "search strings for this scene).\n\n"
    "Order the queries from most specific to most generic, and write each as three "
    "to six concrete filmable nouns. They are typed into a stock library that "
    "matches words in clip titles and tags, not meaning, so a query only works if "
    "real footage of that exact object plausibly exists.\n\n"
    "Good, for a scene about flash memory wearing out:\n"
    '  ["nand flash memory wafer", "silicon chip macro close up", "microscope circuit"]\n'
    "Bad, for the same scene:\n"
    '  ["capacity sticker"] - a literal keyword match for nothing in the scene; a '
    "stock library returns a warehouse shelf labelled with a weight limit.\n"
    '  ["data degradation"] - abstract; nothing can be filmed.\n'
    '  ["the future of storage"] - a headline, not a shot.\n'
    '  ["business team handshake"] - generic stock filler that fits any topic.\n\n'
    "Concrete objects, places and actions only. No abstract concepts, no text on "
    "screen, no named people or brands."
)


class DraftScene(BaseModel):
    """One scene as the model is asked to write it."""

    model_config = ConfigDict(extra="forbid")

    narration: str = Field(min_length=1)
    visual_queries: list[str] = Field(min_length=1)

    @model_validator(mode="before")
    @classmethod
    def _accept_a_single_query(cls, data: Any) -> Any:
        """Promote a bare `visual_query` string to the list this now expects.

        Structured output is a request, not a guarantee: several free-tier endpoints
        honour the schema loosely, and a model that answers the M1 shape would
        otherwise fail validation, burn the repair retry, and cost the whole
        provider its turn in the chain — for a reply that is perfectly usable with
        one rung of ladder instead of three.
        """
        if isinstance(data, dict) and "visual_query" in data and "visual_queries" not in data:
            promoted = dict(data)
            single = promoted.pop("visual_query")
            promoted["visual_queries"] = [single] if isinstance(single, str) else single
            return promoted
        return data


class DraftScript(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenes: list[DraftScene] = Field(min_length=1)


def scene_schema(scene_count: int) -> dict[str, Any]:
    """The JSON schema sent to the provider, fully inlined."""
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["scenes"],
        "properties": {
            "scenes": {
                "type": "array",
                "minItems": scene_count,
                "maxItems": scene_count,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["narration", "visual_queries"],
                    "properties": {
                        "narration": {"type": "string"},
                        "visual_queries": {
                            "type": "array",
                            "minItems": MIN_QUERIES_PER_SCENE,
                            "maxItems": MAX_QUERIES_PER_SCENE,
                            "items": {"type": "string"},
                        },
                    },
                },
            }
        },
    }


def build_prompt(project: Project, template: Template, scene_count: int) -> tuple[str, str]:
    """`(system, user)` for the script call. The template owns the editorial voice."""
    system = f"{template.system_prompt.strip()}\n\n{_OUTPUT_CONTRACT}"
    words = round(project.target_minutes * template.words_per_minute)
    user = "\n".join(
        [
            f"Topic: {project.topic}",
            f"Language: {project.language}",
            f"Scenes: exactly {scene_count}",
            f"Beats, in order: {', '.join(template.structure)}",
            f"Total spoken length: about {words} words.",
        ]
    )
    return system, user


def _extract_json(text: str) -> Any:
    """Parse the model's reply, tolerating a markdown fence or a sentence around it."""
    stripped = text.strip()
    start, end = stripped.find("{"), stripped.rfind("}")
    if start == -1 or end <= start:
        raise ProviderResponseError("no JSON object in the reply")
    try:
        return json.loads(stripped[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ProviderResponseError(f"reply was not valid JSON: {exc}") from exc


def parse_script(text: str) -> DraftScript:
    try:
        return DraftScript.model_validate(_extract_json(text))
    except ValidationError as exc:
        raise ProviderResponseError(f"reply did not match the script schema: {exc}") from exc


def _repair_prompt(user: str, reply: str, problem: str) -> str:
    return (
        f"{user}\n\n"
        "Your previous reply could not be used. The error was:\n"
        f"{problem}\n\n"
        "Your previous reply was:\n"
        f"{reply}\n\n"
        f"{_OUTPUT_CONTRACT} Return the corrected JSON object only."
    )


def _draft(
    name: str,
    llm: LLMProvider,
    *,
    system: str,
    user: str,
    schema: dict[str, Any],
) -> DraftScript:
    """One provider's best effort: the call, then at most one repair retry."""

    def ask(prompt: str) -> str:
        return llm.generate(
            system=system,
            user=prompt,
            json_schema=schema,
            temperature=TEMPERATURE,
            max_tokens=MAX_TOKENS,
        ).text

    reply = ask(user)
    try:
        return parse_script(reply)
    except ProviderResponseError as first:
        repaired = ask(_repair_prompt(user, reply, str(first)))
    try:
        return parse_script(repaired)
    except ProviderResponseError as second:
        raise ProviderResponseError(
            f"{name} returned an unusable script twice; last error: {second}"
        ) from second


def assign_beats(structure: Sequence[str], count: int) -> list[str]:
    """Which of the template's beats each of `count` scenes was written for.

    The script stage asks the model to follow `structure` **in order**, so the beat
    a scene belongs to can be read off its position — no second model call, no extra
    field in the reply schema, nothing to disagree with itself.

    The mapping is not a bare `floor(i * beats / count)`. That has a real failure
    mode: with fewer scenes than beats it runs off the end of the list before it
    reaches the closing beat, so the scene the model wrote as the close is labelled
    `implication` and drops out of the Short — the one scene a Short can least
    afford to lose. Instead the **first** scene takes the opening beat, the **last**
    takes the closing beat, and everything in between spreads evenly over the beats
    between them. That also matches how these prompts are written: an opener and a
    landing are one scene each, and the body is what expands.

    Its remaining failure mode is honest and unavoidable: it is positional, so a
    model that reorders or front-loads the beats gets a scene labelled wrongly.
    That is why the label only ever produces a *default* the storyboard shows and a
    person can override — see `apply_short_defaults`.
    """
    if count <= 0:
        return []
    beats = list(structure)
    if count == 1 or len(beats) == 1:
        return [beats[0]] * count
    # With only two beats there is no "between", so the middles split across both.
    middle = beats[1:-1] or beats
    inner = count - 2
    return [
        beats[0],
        *(middle[index * len(middle) // inner] for index in range(inner)),
        beats[-1],
    ]


def apply_short_defaults(
    scenes: Iterable[Scene],
    template: Template,
    *,
    pinned: Mapping[str, bool] | None = None,
) -> None:
    """Set `in_short` from each scene's beat — unless a person has already decided.

    Three deliberate no-ops, each of them a compatibility guarantee:

    * `pinned` is applied **first and wins outright**. It carries the ticks a person
      set on a previous script across a rewrite, by scene id, and re-marks them
      pinned so the next run cannot undo them either. The narration under a carried
      tick has changed, which is a real cost — but it is the smaller one. A tool
      that silently re-ticks a scene the owner unticked is fighting them, and they
      would only find out after publishing.
    * A template with no `short_beats` has no opinion, so nothing moves. That is
      exactly the pre-M3 behaviour, kept for every template that predates the field.
    * A scene with no recorded `beat` — every scene in a `project.json` written
      before M3 Task 22 — is left as it was found rather than re-selected on the
      first run after an upgrade.
    """
    scenes = list(scenes)
    if pinned:
        for scene in scenes:
            if scene.id in pinned:
                scene.in_short = pinned[scene.id]
                scene.short_pinned = True
    if not template.short_beats:
        return
    wanted = set(template.short_beats)
    for scene in scenes:
        if scene.short_pinned or scene.beat is None:
            continue
        scene.in_short = scene.beat in wanted


def pinned_short_choices(scenes: Iterable[Scene]) -> dict[str, bool]:
    """The `in_short` ticks a person set, by scene id. The rest are the machine's."""
    return {scene.id: scene.in_short for scene in scenes if scene.short_pinned}


def _scene_queries(item: DraftScene) -> list[str]:
    """The scene's searches, trimmed, blanks dropped, order kept.

    A model that pads its array with `""` must not hand the visuals stage a rung
    that searches for nothing — it would spend a Pexels request to learn that.
    """
    queries = [text for text in (query.strip() for query in item.visual_queries) if text]
    return queries or [item.narration.strip()]


def _to_scenes(draft: DraftScript, template: Template) -> list[Scene]:
    beats = assign_beats(template.structure, len(draft.scenes))
    scenes = []
    for index, (item, beat) in enumerate(zip(draft.scenes, beats, strict=True), start=1):
        query, *alternates = _scene_queries(item)
        scenes.append(
            Scene(
                id=SCENE_ID_FORMAT.format(index),
                narration=item.narration.strip(),
                # The first query is the scene's own; the rest are the ladder's rungs.
                visual=SceneVisual(query=query, alt_queries=alternates),
                beat=beat,
            )
        )
    return scenes


def run_script(project: Project, deps: StageDeps) -> StageResult:
    """Write `project.scenes` from the topic and template. One unit: `script:all`."""
    template = load_template(project.template)
    key = stage_key(STAGE)
    current = hash_inputs(
        topic=project.topic,
        # The template's *content*, not its name: editing a template's prompt has to
        # invalidate the scripts it wrote, and `Template` carries no version field.
        # Content the script is not a function of is excluded — see
        # `Template.script_fingerprint`, which is also what `runner` hashes.
        template=template.script_fingerprint(),
        target_minutes=project.target_minutes,
        language=project.language,
        model=deps.leading_name("llm"),
    )
    if not deps.stage_cache.is_stale(key, current) and project.scenes:
        return StageResult(changed=False, skipped_units=1)

    scene_count = template.target_scene_count(project.target_minutes)
    system, user = build_prompt(project, template, scene_count)
    schema = scene_schema(scene_count)
    draft = call_chain(
        deps,
        "llm",
        lambda name, llm: _draft(name, llm, system=system, user=user, schema=schema),
        advance_on=_SCRIPT_ADVANCE_ON,
    )

    # Taken before the rewrite: these are the only decisions on the old scene list
    # worth carrying, because they are the only ones a person made by hand.
    pinned = pinned_short_choices(project.scenes)
    project.scenes = _to_scenes(draft, template)
    apply_short_defaults(project.scenes, template, pinned=pinned)
    deps.stage_cache.mark(key, current)
    deps.stage_cache.save()
    deps.store.save(project)
    return StageResult(changed=True, skipped_units=0)

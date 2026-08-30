"""The script stage: one LLM call turns a topic and a template into scenes.

The model is asked for JSON against an **inlined** schema — no `$ref`/`$defs`, which
several structured-output endpoints reject and which nothing downstream needs — and
the reply is validated with pydantic. A reply that does not validate earns exactly
one repair retry carrying the validation error back to the model; a provider that
fails that twice is abandoned for the next one in the chain, because a model that
cannot follow a schema twice will not follow it a third time.
"""

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

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

_OUTPUT_CONTRACT = (
    "Reply with a single JSON object and nothing else: no prose, no markdown fence. "
    'It has one key, "scenes", an array of objects with exactly two string keys: '
    '"narration" (what the narrator says, spoken prose only) and "visual_query" '
    "(three to six concrete filmable nouns for a stock-footage search)."
)


class DraftScene(BaseModel):
    """One scene as the model is asked to write it."""

    model_config = ConfigDict(extra="forbid")

    narration: str = Field(min_length=1)
    visual_query: str = Field(min_length=1)


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
                    "required": ["narration", "visual_query"],
                    "properties": {
                        "narration": {"type": "string"},
                        "visual_query": {"type": "string"},
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


def _to_scenes(draft: DraftScript) -> list[Scene]:
    return [
        Scene(
            id=SCENE_ID_FORMAT.format(index),
            narration=item.narration.strip(),
            visual=SceneVisual(query=item.visual_query.strip()),
        )
        for index, item in enumerate(draft.scenes, start=1)
    ]


def run_script(project: Project, deps: StageDeps) -> StageResult:
    """Write `project.scenes` from the topic and template. One unit: `script:all`."""
    template = load_template(project.template)
    key = stage_key(STAGE)
    current = hash_inputs(
        topic=project.topic,
        # The template's *content*, not its name: editing a template's prompt has to
        # invalidate the scripts it wrote, and `Template` carries no version field.
        template=template.model_dump_json(),
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

    project.scenes = _to_scenes(draft)
    deps.stage_cache.mark(key, current)
    deps.stage_cache.save()
    deps.store.save(project)
    return StageResult(changed=True, skipped_units=0)

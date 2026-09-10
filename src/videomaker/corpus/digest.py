"""The brief: one LLM call turns a passage into plain language, and says so.

**A brief is a retelling, and every surface that shows one must label it** with the
source text one click away. That label is the feature, not a disclaimer — a reader
who asks to hear John 3 and is read a language model's paraphrase has been handed
something the tool cannot stand behind (`docs/multimodal-reader-design.md` §3).
The `listen` mode never touches this module: narration is the source text.

The call follows `pipeline/script.py`'s pattern exactly — an inlined JSON schema
with no `$ref`/`$defs`, pydantic validation, exactly one repair retry carrying the
validation error back, then advance the chain. A model that cannot follow a schema
twice will not follow it a third time.

Cached at `library/<work>/derived/brief/<brief_key>.json`, keyed by the unit's
text, the model name and `PROMPT_VERSION`. Bumping `PROMPT_VERSION` is what
re-writes every brief on disk: it is the only way to change the prompt without
leaving old answers behind that no longer match what the prompt asks for.
"""

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from videomaker.cache import _write_atomic, hash_inputs
from videomaker.corpus.importer import DERIVED_DIRNAME, work_dir
from videomaker.corpus.models import UnitText
from videomaker.pipeline.base import ADVANCE_ON, StageDeps, call_chain
from videomaker.providers.base import LLMProvider
from videomaker.providers.errors import ProviderResponseError

BRIEF_DIRNAME = "brief"

#: Bump to re-write every brief on disk. The key covers it, so an old answer
#: written to a different question is never served for a new one.
PROMPT_VERSION = 1

#: A brief that runs past this is not a brief. 120 words is roughly 45 seconds of
#: reading — short enough that a reader takes it before the passage rather than
#: instead of it.
MAX_SUMMARY_WORDS = 120

TEMPERATURE = 0.3  # a retelling, not a composition
MAX_TOKENS = 1024

#: A malformed reply is a *response* problem, so it advances the chain too — the
#: same rule `pipeline/script.py` uses, for the same reason.
_BRIEF_ADVANCE_ON = (*ADVANCE_ON, ProviderResponseError)

_SYSTEM = (
    "You write short, plain-language briefs on passages of text for readers who "
    "want their bearings before they read the passage itself.\n\n"
    "Everything you write must be traceable to the passage you are given. Do not "
    "add doctrine, interpretation, historical background, or any name, place or "
    "event the passage does not mention. If the passage does not say something, "
    "it is not in the brief. You are describing what is on the page, in ordinary "
    "modern words, for someone who has not read it yet.\n\n"
    "Reply with a single JSON object and nothing else: no prose, no markdown "
    "fence. Its keys are exactly:\n"
    f'  "summary" - what the passage says, in plain language, at most '
    f"{MAX_SUMMARY_WORDS} words.\n"
    '  "people" - the people named or clearly present in the passage. Empty if none.\n'
    '  "places" - the places the passage names. Empty if none.\n'
    '  "turn" - one sentence naming what changes in this passage.\n'
)


class Brief(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ref_key: str
    summary: str = Field(min_length=1)
    people: list[str] = Field(default_factory=list)
    places: list[str] = Field(default_factory=list)
    turn: str = ""
    model: str = ""

    @field_validator("summary")
    @classmethod
    def _short_enough_to_be_a_brief(cls, value: str) -> str:
        words = len(value.split())
        if words > MAX_SUMMARY_WORDS:
            raise ValueError(f"the summary is {words} words; the limit is {MAX_SUMMARY_WORDS}")
        return value


def brief_schema() -> dict[str, Any]:
    """Fully inlined: several structured-output endpoints reject `$ref`/`$defs`."""
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["summary", "people", "places", "turn"],
        "properties": {
            "summary": {"type": "string"},
            "people": {"type": "array", "items": {"type": "string"}},
            "places": {"type": "array", "items": {"type": "string"}},
            "turn": {"type": "string"},
        },
    }


def brief_key(unit: UnitText, *, model: str, prompt_version: int = PROMPT_VERSION) -> str:
    return hash_inputs(text=unit.plain, model=model, prompt_version=prompt_version)


def brief_path(workspace_dir, unit: UnitText, key: str):
    return work_dir(workspace_dir, unit.ref.work_id) / DERIVED_DIRNAME / BRIEF_DIRNAME / f"{key}.json"


def build_prompt(unit: UnitText) -> tuple[str, str]:
    user = "\n".join([f"Passage: {unit.title}", "", unit.plain])
    return _SYSTEM, user


def _extract_json(text: str) -> Any:
    stripped = text.strip()
    start, end = stripped.find("{"), stripped.rfind("}")
    if start == -1 or end <= start:
        raise ProviderResponseError("no JSON object in the reply")
    try:
        return json.loads(stripped[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ProviderResponseError(f"reply was not valid JSON: {exc}") from exc


def parse_brief(text: str, *, ref_key: str, model: str) -> Brief:
    try:
        payload = _extract_json(text)
        if isinstance(payload, dict):
            payload = {**payload, "ref_key": ref_key, "model": model}
        return Brief.model_validate(payload)
    except ValidationError as exc:
        raise ProviderResponseError(f"reply did not match the brief schema: {exc}") from exc


def _repair_prompt(user: str, reply: str, problem: str) -> str:
    return (
        f"{user}\n\n"
        "Your previous reply could not be used. The error was:\n"
        f"{problem}\n\n"
        "Your previous reply was:\n"
        f"{reply}\n\n"
        "Return the corrected JSON object only."
    )


def _ask(name: str, llm: LLMProvider, unit: UnitText, *, model_hint: str) -> Brief:
    """One provider's best effort: the call, then at most one repair retry."""
    system, user = build_prompt(unit)
    schema = brief_schema()

    def ask(prompt: str):
        return llm.generate(
            system=system,
            user=prompt,
            json_schema=schema,
            temperature=TEMPERATURE,
            max_tokens=MAX_TOKENS,
        )

    result = ask(user)
    try:
        return parse_brief(result.text, ref_key=unit.ref.key(), model=result.model or model_hint)
    except ProviderResponseError as first:
        repaired = ask(_repair_prompt(user, result.text, str(first)))
    try:
        return parse_brief(repaired.text, ref_key=unit.ref.key(), model=repaired.model or model_hint)
    except ProviderResponseError as second:
        raise ProviderResponseError(
            f"{name} returned an unusable brief twice; last error: {second}"
        ) from second


def build_brief(unit: UnitText, deps: StageDeps) -> Brief:
    """The brief for `unit`, from cache when it exists.

    The cache is keyed on the *leading* provider's name, decided before any
    provider is built — the same discipline as every stage hash (`StageDeps`).
    """
    model = deps.leading_name("llm")
    key = brief_key(unit, model=model)
    path = brief_path(deps.settings.workspace_dir, unit, key)
    if path.is_file():
        try:
            return Brief.model_validate_json(path.read_text())
        except ValidationError:
            # A brief written by an older shape of this model is not worth a
            # crash; re-asking costs one call and leaves a valid file behind.
            pass
    brief = call_chain(
        deps,
        "llm",
        lambda name, llm: _ask(name, llm, unit, model_hint=name),
        advance_on=_BRIEF_ADVANCE_ON,
    )
    _write_atomic(path, brief.model_dump_json(indent=1))
    return brief

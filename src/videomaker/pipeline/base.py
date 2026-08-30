"""Shared plumbing every pipeline stage needs: dependencies, results, chain walking."""

import hashlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from videomaker.cache import HASH_LEN, ResponseCache, StageCache
from videomaker.config import Settings
from videomaker.models import Project
from videomaker.project import ProjectStore
from videomaker.providers import get_provider, resolve_chain
from videomaker.providers.errors import ProviderConfigError, ProviderError, QuotaExceeded
from videomaker.providers.ratelimit import QuotaTracker

#: A provider raising one of these cannot serve us at all: walk on to the next one
#: in the chain rather than failing the stage.
ADVANCE_ON: tuple[type[ProviderError], ...] = (QuotaExceeded, ProviderConfigError)

#: Attributes a provider may declare so the whole run shares one cache and one budget.
_INJECTED = ("cache", "quota")

DIGEST_CHUNK_BYTES = 1 << 20


@dataclass(frozen=True)
class StageResult:
    """What one stage did. `skipped_units` are the units the hash engine spared."""

    changed: bool = False
    skipped_units: int = 0


@dataclass
class StageDeps:
    """Everything a stage needs that is not the project itself.

    Constructed once per run (by `runner.py`) and threaded through every stage, so
    a single quota budget and a single response cache cover the whole pipeline.
    """

    settings: Settings
    store: ProjectStore
    stage_cache: StageCache
    response_cache: ResponseCache
    quota: QuotaTracker
    #: Provider instances live as long as the run: building `WhisperModel` per scene
    #: would make alignment cost realtime (M0 finding 2).
    instances: dict[tuple[str, str], object] = field(default_factory=dict, repr=False)

    def chain(self, kind: str) -> list[str]:
        """The configured provider names for `kind`, best first."""
        return resolve_chain(kind, self.settings)

    def leading_name(self, kind: str) -> str:
        """The name the chain would try first.

        Stage hashes record this rather than a resolved model id: staleness has to
        be decided *before* any provider is built or called, and Groq's concrete
        model id is only known after a live catalogue lookup (M0 finding 1).
        """
        names = self.chain(kind)
        if not names:
            raise ProviderConfigError(f"no {kind} provider is configured")
        return names[0]

    def iter_providers(self, kind: str) -> Iterator[tuple[str, object]]:
        """Yield `(name, provider)` down the chain, skipping ones that cannot be built."""
        names = self.chain(kind)
        if not names:
            raise ProviderConfigError(f"no {kind} provider is configured")
        problems: list[str] = []
        usable = 0
        for name in names:
            try:
                instance = self._instance(kind, name)
            except ADVANCE_ON as exc:
                problems.append(f"{name}: {exc}")
                continue
            usable += 1
            yield name, instance
        if usable == 0:
            detail = "; ".join(problems) or "none could be built"
            raise ProviderConfigError(f"no usable {kind} provider in {names}: {detail}")

    def provider(self, kind: str) -> object:
        """The first provider in the chain that can be built."""
        for _name, instance in self.iter_providers(kind):
            return instance
        raise ProviderConfigError(f"no usable {kind} provider configured")  # pragma: no cover

    def _instance(self, kind: str, name: str) -> object:
        key = (kind, name)
        instance = self.instances.get(key)
        if instance is None:
            instance = get_provider(kind, name, self.settings)
            for attribute, value in zip(_INJECTED, (self.response_cache, self.quota), strict=True):
                if hasattr(instance, attribute):
                    setattr(instance, attribute, value)
            self.instances[key] = instance
        return instance


def call_chain[T](
    deps: StageDeps,
    kind: str,
    call: Callable[[str, object], T],
    *,
    advance_on: tuple[type[ProviderError], ...] = ADVANCE_ON,
) -> T:
    """Run `call(name, provider)` down the chain, advancing past providers that fail.

    Only `advance_on` failures move to the next provider; anything else (a
    `TransientError` a provider already retried, an OSError) fails the stage, because
    burning the fallback on a fault the next provider would hit too helps nobody.
    """
    problems: list[str] = []
    for name, instance in deps.iter_providers(kind):
        try:
            return call(name, instance)
        except advance_on as exc:
            problems.append(f"{name}: {exc}")
    raise ProviderError(f"every {kind} provider failed: {'; '.join(problems)}")


def project_root(deps: StageDeps, project: Project) -> Path:
    return deps.store.path_for(project.id)


def relative_to_project(deps: StageDeps, project: Project, path: Path) -> str:
    """Paths stored in `project.json` are relative, so project folders stay movable."""
    root = project_root(deps, project).resolve()
    return Path(path).resolve().relative_to(root).as_posix()


def content_hash(path: Path) -> str:
    """Digest of a file's bytes — how a stage notices its input artefact changed."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while chunk := handle.read(DIGEST_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()[:HASH_LEN]

"""The visuals stage: one downloaded asset per scene.

Stock is searched **first** and an AI image is generated only when no stock source
can serve the scene. That order is not an aesthetic preference: Workers AI bills
automatically once the free neuron cap is passed rather than failing (M0 finding 6),
so every scene real footage can cover is a scene that costs nothing.

`VisualKind.AUTO` is resolved through the template's `visual_kind_order` on every
run and is deliberately **not** written back to the scene: the stage hash covers the
scene's declared kind, so persisting the resolved one would make the very next run
see a changed input and re-fetch everything it just fetched.
"""

from pathlib import Path
from urllib.parse import urlparse

from videomaker.cache import hash_inputs, stage_key
from videomaker.models import Aspect, AssetRef, Project, Scene, StockResult, VisualKind
from videomaker.pipeline.base import (
    ADVANCE_ON,
    StageDeps,
    StageResult,
    call_chain,
    project_root,
)
from videomaker.providers.base import ImageProvider, StockProvider
from videomaker.providers.errors import ProviderConfigError, ProviderError
from videomaker.templates import load_template

STAGE = "visuals"
#: Enough for a storyboard picker (M2) without downloading footage nobody watches.
MAX_CANDIDATES = 4
ASSET_STEM = "asset"
VIDEO_SUFFIX = ".mp4"
PHOTO_SUFFIX = ".jpg"
MAX_SUFFIX_LEN = 5
#: M1 is wide only; M3 adds the vertical crop and its own orientation.
ASPECT = Aspect.WIDE
ORIENTATION = "landscape"
MAX_HEIGHT = 1080

#: Which provider chain serves each visual kind.
PROVIDER_KIND: dict[VisualKind, str] = {
    VisualKind.STOCK_VIDEO: "stock",
    VisualKind.STOCK_PHOTO: "stock",
    VisualKind.AI_IMAGE: "image",
}


class NoResults(ProviderError):
    """This provider works, it just has nothing for this query — try the next one."""


#: An empty-handed provider advances the chain like an unusable one does.
_SEARCH_ADVANCE_ON = (*ADVANCE_ON, NoResults)


def resolve_kinds(scene: Scene, kind_order: list[VisualKind]) -> list[VisualKind]:
    """The kinds to try, in order. `AUTO` means "whatever the template prefers"."""
    if scene.visual.kind is not VisualKind.AUTO:
        return [scene.visual.kind]
    return [kind for kind in kind_order if kind is not VisualKind.AUTO]


def provider_names(deps: StageDeps, kinds: list[VisualKind]) -> list[str]:
    """Leading chain names for the provider kinds this scene could use.

    Names, not resolved model ids: staleness has to be decided without a network
    call. A kind with no configured chain contributes nothing — it cannot serve the
    scene, so it cannot change its output either.
    """
    names = []
    for provider_kind in dict.fromkeys(PROVIDER_KIND[kind] for kind in kinds):
        try:
            names.append(f"{provider_kind}:{deps.leading_name(provider_kind)}")
        except ProviderConfigError:
            continue
    return names


def scene_hash(scene: Scene, kinds: list[VisualKind], providers: list[str]) -> str:
    return hash_inputs(
        query=scene.visual.query,
        # The resolved order, so editing a template's `visual_kind_order` invalidates.
        kind=[kind.value for kind in kinds],
        # A longer scene needs a longer clip: `duration_s` is a search filter.
        duration_s=scene.duration_s,
        provider=providers,
    )


def asset_suffix(result: StockResult) -> str:
    """File extension for a download, taken from the URL and sanity-checked."""
    suffix = Path(urlparse(result.download_url).path).suffix
    if 1 < len(suffix) <= MAX_SUFFIX_LEN and suffix[1:].isalnum():
        return suffix.lower()
    return VIDEO_SUFFIX if result.duration_s is not None else PHOTO_SUFFIX


def _offer(result: StockResult) -> AssetRef:
    """A candidate that was found but not downloaded; M2 fetches it if it is picked."""
    return AssetRef(
        provider=result.provider,
        source_id=result.source_id,
        source_url=result.source_url,
        local_path="",
        width=result.width,
        height=result.height,
        duration_s=result.duration_s,
        attribution=result.attribution,
        license=result.license,
    )


def _clear_old_assets(scene_dir: Path, keep: Path) -> None:
    """Drop a previous run's `asset.mp4` when this run wrote an `asset.jpg`."""
    for path in scene_dir.glob(f"{ASSET_STEM}.*"):
        if path != keep and path.is_file():
            path.unlink()


def _fetch_stock(
    deps: StageDeps, project: Project, scene: Scene, kind: VisualKind
) -> list[AssetRef]:
    scene_dir = deps.store.scene_dir(project, scene.id)

    def call(name: str, stock: StockProvider) -> list[AssetRef]:
        results = stock.search(
            query=scene.visual.query,
            kind=kind,
            min_duration_s=scene.duration_s or 0.0,
            orientation=ORIENTATION,
            per_page=MAX_CANDIDATES,
        )[:MAX_CANDIDATES]
        if not results:
            raise NoResults(f"{name} has no {kind.value} for {scene.visual.query!r}")
        # Search and download share one provider instance: a `StockResult` is only
        # meaningful to the library that produced it.
        out_path = scene_dir / f"{ASSET_STEM}{asset_suffix(results[0])}"
        chosen = stock.download(results[0], out_path, max_height=MAX_HEIGHT)
        _clear_old_assets(scene_dir, out_path)
        return [chosen, *(_offer(result) for result in results[1:])]

    return call_chain(deps, "stock", call, advance_on=_SEARCH_ADVANCE_ON)


def _generate_image(deps: StageDeps, project: Project, scene: Scene) -> list[AssetRef]:
    scene_dir = deps.store.scene_dir(project, scene.id)
    out_path = scene_dir / f"{ASSET_STEM}{PHOTO_SUFFIX}"

    def call(_name: str, image: ImageProvider) -> list[AssetRef]:
        # Composition guidance for the 1024² square (M0 finding 5) belongs to the
        # image provider, which knows its own model's shape.
        ref = image.generate_image(prompt=scene.visual.query, out_path=out_path, aspect=ASPECT)
        _clear_old_assets(scene_dir, out_path)
        return [ref]

    return call_chain(deps, "image", call)


def _fetch(
    deps: StageDeps, project: Project, scene: Scene, kinds: list[VisualKind]
) -> list[AssetRef]:
    """Walk the kinds in order and take the first that yields something."""
    problems: list[str] = []
    for kind in kinds:
        try:
            if PROVIDER_KIND[kind] == "stock":
                return _fetch_stock(deps, project, scene, kind)
            return _generate_image(deps, project, scene)
        except ProviderError as exc:
            problems.append(f"{kind.value}: {exc}")
    detail = "; ".join(problems) or "no visual kinds to try"
    raise ProviderError(f"no visual for scene {scene.id} ({scene.visual.query!r}): {detail}")


def _is_fresh(deps: StageDeps, project: Project, scene: Scene, key: str, current: str) -> bool:
    """Fresh means the hash matches *and* the chosen asset is still on disk."""
    chosen = scene.visual.chosen
    if deps.stage_cache.is_stale(key, current) or chosen is None or not chosen.local_path:
        return False
    return (project_root(deps, project) / chosen.local_path).is_file()


def run_visuals(project: Project, deps: StageDeps) -> StageResult:
    """Find and download a visual for every stale, unlocked scene. Unit: `visuals:sNN`."""
    kind_order = load_template(project.template).visual_kind_order
    changed = False
    skipped = 0

    for scene in project.scenes:
        if scene.locked:
            # An editor picked this shot deliberately; staleness does not outrank that.
            skipped += 1
            continue

        kinds = resolve_kinds(scene, kind_order)
        key = stage_key(STAGE, scene.id)
        current = scene_hash(scene, kinds, provider_names(deps, kinds))
        if _is_fresh(deps, project, scene, key, current):
            skipped += 1
            continue

        # Providers set `local_path` with `providers.assets.project_relative`, so the
        # refs come back already anchored on the project folder.
        refs = _fetch(deps, project, scene, kinds)
        scene.visual.candidates = refs[:MAX_CANDIDATES]
        scene.visual.chosen = scene.visual.candidates[0]
        scene.error = None
        deps.stage_cache.mark(key, current)
        changed = True

    if changed:
        deps.stage_cache.save()
        deps.store.save(project)
    return StageResult(changed=changed, skipped_units=skipped)

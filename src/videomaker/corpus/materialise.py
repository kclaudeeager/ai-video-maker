"""A passage becomes an ordinary project.

**This is the one place the reader crosses into the pipeline, and it crosses all
the way.** A reader asking to watch a chapter is a creator starting a project, so
what they get is a `Project` like any other: the same `STAGE_ORDER`, the same
three gates, the same review screens. The curation argument the whole tool is
founded on applies to them too, which is why watching does not bypass a gate and
does not add a fourth.

Everything *up to* that crossing stays as it was. Reading, briefing and listening
still create no project and enter no stage; `web/workspace.py` still projects the
two stores onto one list without merging them. The invariant was never "the
reader may not make a project" — it was "reading must not be able to move one",
and that is unchanged.

**Re-materialising the same passage returns the project already made for it.**
The unit key is the identity, so pressing Watch twice does not leave two
half-finished projects behind; `Project.source` is where that identity is
recorded, and it deliberately feeds no fingerprint.
"""

from videomaker.corpus.models import UnitRef, UnitText
from videomaker.corpus.refs import book_label, format_reference
from videomaker.models import Project, SourceRef
from videomaker.project import ProjectStore

#: What a materialised project is written from until somebody changes it. The
#: reader has no template picker: a passage is not a topic, and asking which
#: editorial voice to use before the reader has seen a single scene is a question
#: too early. Gate 1 is where that conversation belongs.
DEFAULT_TEMPLATE = "tech_explainer"

#: How long a chapter's video runs, before anyone edits it. Two minutes is the
#: pipeline's own default and there is no better guess available here: the honest
#: number depends on the passage, and the script stage is what discovers it.
DEFAULT_MINUTES = 2.0


def source_for(ref: UnitRef) -> SourceRef:
    return SourceRef(work_id=ref.work_id, unit_key=ref.key())


def existing_for(store: ProjectStore, ref: UnitRef) -> Project | None:
    """The project already materialised from `ref`, if there is one.

    A linear scan of the workspace, which is the right shape here: a workspace
    holds tens of projects, the alternative is an index that can disagree with
    the projects themselves, and this runs when somebody presses a button.
    """
    key = ref.key()
    for project_id in store.list_ids():
        try:
            project = store.load(project_id)
        except (OSError, ValueError):
            continue
        if project.source is not None and project.source.unit_key == key:
            return project
    return None


def materialise(
    ref: UnitRef,
    *,
    unit: UnitText,
    store: ProjectStore,
    template: str = DEFAULT_TEMPLATE,
    voice: str = "",
) -> Project:
    """The project for this passage, made if it does not exist yet.

    `topic` is the formatted reference rather than the passage itself: the script
    stage writes *about* a subject, and handing it 800 words of scripture as a
    topic would produce a script about the shape of the text. The verbatim path —
    where the narration is the passage — is the next plan's, and `Project.source`
    is what will let it find its way back here.

    `folder` files it under the work and book, so a reader who watches five
    chapters of John has one folder rather than five loose projects.
    """
    found = existing_for(store, ref)
    if found is not None:
        return found
    project = store.create(
        format_reference(ref),
        template,
        target_minutes=DEFAULT_MINUTES,
        folder=f"{ref.work_id}/{book_label(ref.book)}",
        **({"voice": voice} if voice else {}),
    )
    project.source = source_for(ref)
    project.thumbnail_text = unit.title
    store.save(project)
    return project

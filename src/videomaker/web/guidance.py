"""The deterministic next-step panel: where you are, what you are judging, one button.

Most newcomer confusion is *"what do I do now?"*, and the application already
knows. `derive_status` walks the stages in order and stops at the first stale
unit or unapproved gate; `GATE_BEFORE` says which gate guards which stage. This
module walks **the same loop, in the same order** and returns what that stop
means in plain language.

That is the whole design decision, and it is the reason this is not a chat:

* **zero LLM tokens and zero latency** — it is a dict lookup over a walk the page
  was doing anyway;
* **it cannot disagree with the pipeline**, because it is not a second model of
  the state machine. If `run_pipeline` would stop at the storyboard gate, this
  says "storyboard gate", by construction rather than by keeping a copy in step.

`GATE_ASK` is presentation, exactly like `projects.STAGE_LABELS`: the *set* of
gates still comes from `GATE_BEFORE`, so a gate added later shows up here with
its `GATE_REVIEW` sentence rather than silently vanishing from the panel.
"""

from dataclasses import dataclass

from videomaker.cache import STAGE_ORDER, StageCache
from videomaker.models import Project
from videomaker.runner import GATE_BEFORE, GATE_REVIEW, stage_is_current

#: gate -> what the human is being asked to judge, said to the human rather than
#: to the maintainer. `GATE_REVIEW` names files (`project.json`, `scenes/sNN/`)
#: because it is printed by the CLI next to a path; on a page that is showing the
#: thing itself, the path is noise. Anything not listed falls back to
#: `GATE_REVIEW`, so this can never be the reason a gate goes unexplained.
GATE_ASK: dict[str, str] = {
    "script": (
        "Read the narration the model wrote and fix anything wrong before a "
        "single word is spoken aloud."
    ),
    "storyboard": (
        "Watch each shot against its narration and swap in a better one where "
        "the search missed. Nothing is captioned or assembled until you do."
    ),
    "preview": (
        "Watch the whole cut at 480p. This is the last stop before the machine "
        "spends real time on the full-size render."
    ),
}

#: gate -> the page that clears it, relative to `/projects/{id}`.
GATE_PATH: dict[str, str] = {
    "script": "script",
    "storyboard": "storyboard",
    "preview": "preview",
}

#: What each stage is doing, for the "a run is working" line. Presentation only.
STAGE_DOING: dict[str, str] = {
    "script": "writing the script",
    "voice": "speaking every scene",
    "align": "timing the words against the takes",
    "visuals": "finding a shot for every scene",
    "captions": "building the subtitles",
    "assemble": "joining the scenes into one timeline",
    "render": "encoding the finished videos",
}


@dataclass(frozen=True)
class NextStep:
    """One line of guidance, and the single control that acts on it.

    `kind` is the panel's tone, not its content: `review` is the human's turn
    (warm), `run` and `busy` are the machine's (cool), `done` is the end.
    """

    kind: str
    #: "Gate 1 of 3 · Script", "Step 4 of 7 · Visuals" — where you are.
    where: str
    #: One sentence: what you are being asked to judge, or what will happen.
    ask: str
    #: The one primary control. Empty `action_url` means there is nothing to press.
    action_label: str = ""
    action_url: str = ""
    #: "post" needs a real form; "get" is a link. Both work with JavaScript off.
    action_method: str = "get"
    #: The gate this step is about, or "" — used to highlight the right rail stop.
    gate: str = ""

    @property
    def tone(self) -> str:
        """The pill/rule colour class. Warm only when it is the human's turn."""
        return {
            "review": "is-review",
            "done": "is-done",
            "busy": "is-busy",
        }.get(self.kind, "is-run")

    @property
    def has_action(self) -> bool:
        return bool(self.action_url)


def _gate_number(gate: str) -> int:
    """1, 2 or 3 — the gate's position in pipeline order, from `GATE_BEFORE`."""
    return list(GATE_BEFORE.values()).index(gate) + 1


def _stage_number(stage: str) -> int:
    return STAGE_ORDER.index(stage) + 1


def next_step(
    project: Project,
    stage_cache: StageCache,
    *,
    busy: bool = False,
    running_stage: str | None = None,
) -> NextStep:
    """What to do next, derived from the same walk `derive_status` performs.

    The loop below is deliberately a line-for-line twin of `derive_status`: same
    order, same two stopping conditions, same precedence of gate-before-stage. A
    cleverer formulation that happened to agree today is exactly the thing that
    drifts, and a guidance panel that disagrees with the runner is worse than no
    guidance panel.
    """
    project_url = f"/projects/{project.id}"

    if busy:
        doing = STAGE_DOING.get(running_stage or "", "working through the pipeline")
        return NextStep(
            kind="busy",
            where="A run is in flight",
            ask=f"The pipeline is {doing}. It will stop on its own at the next gate.",
        )

    for stage in STAGE_ORDER:
        gate = GATE_BEFORE.get(stage)
        if gate is not None and getattr(project.approvals, gate) is None:
            total = len(GATE_BEFORE)
            return NextStep(
                kind="review",
                where=f"Gate {_gate_number(gate)} of {total} · your turn",
                ask=GATE_ASK.get(gate, GATE_REVIEW[gate]),
                action_label=f"Review the {gate}",
                action_url=f"{project_url}/{GATE_PATH.get(gate, gate)}",
                action_method="get",
                gate=gate,
            )
        if not stage_is_current(project, stage_cache, stage):
            doing = STAGE_DOING.get(stage, stage)
            return NextStep(
                kind="run",
                where=f"Step {_stage_number(stage)} of {len(STAGE_ORDER)} · the machine's turn",
                ask=f"Nothing is waiting on you. The next run starts by {doing}.",
                action_label="Run to the next gate",
                action_url=f"{project_url}/advance",
                action_method="post",
            )

    return NextStep(
        kind="done",
        where="Finished · all three gates signed",
        ask="Both cuts are rendered. Download them, or edit a scene to build them again.",
        action_label="Open the finished files",
        action_url=f"{project_url}/render",
        action_method="get",
    )

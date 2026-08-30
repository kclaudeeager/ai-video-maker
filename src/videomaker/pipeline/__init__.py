"""Pipeline stages: script -> voice -> align -> visuals -> captions -> assemble -> render.

Every stage is a plain function ``run_<stage>(project, deps) -> StageResult`` that
mutates `project` in place and reports what it did. Stages are guarded by the
content-hash engine at **unit** granularity (`all`, a scene id, or an aspect), which
is what makes an unchanged re-run cost nothing and an edit to one scene cost one
scene. Nothing here is async: the pipeline runs in a worker thread (M2 needs that),
so every provider ABC is synchronous.
"""

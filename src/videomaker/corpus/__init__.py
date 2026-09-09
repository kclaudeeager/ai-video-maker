"""The library: works, their references, and the text of one addressable unit.

Deliberately import-light. `providers/base.py` imports `corpus.models` to declare
`CorpusProvider`, and `providers/__init__.py` is reached by every CLI invocation —
so anything expensive added to this package is paid for by `videomaker --help`.
Nothing here may import `videomaker.providers`, for the same reason.
"""

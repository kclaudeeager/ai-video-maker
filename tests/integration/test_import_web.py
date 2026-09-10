"""Importing the World English Bible from ebible.org, for real.

Downloads ~5 MB and parses 66 books, so it is `slow`, and it runs only when
`VIDEOMAKER_NETWORK_TESTS=1` is set: the suite is offline by contract, and CI must
not depend on ebible.org answering. It is the test that keeps
`corpus/usfm.py`'s tolerance of grammar errors honest: the Psalms file carries 42
of them (every one a `\\qs Selah` nest, measured 2026-09-10), and the assertion
that "Selah." survives in Psalm 3 is what proves the tolerance keeps the text
rather than merely suppressing the error.
"""

import json
import os

import pytest

from videomaker.corpus.catalogue import CATALOGUE
from videomaker.corpus.importer import import_work, work_dir

PROTESTANT_CANON_CHAPTERS = 1189
NETWORK_OPT_IN = "VIDEOMAKER_NETWORK_TESTS"


@pytest.mark.slow
@pytest.mark.skipif(not os.environ.get(NETWORK_OPT_IN), reason=f"set {NETWORK_OPT_IN}=1 to download")
def test_the_web_imports_with_every_chapter_and_the_selahs_intact(tmp_path):
    work = import_work(CATALOGUE["web"], tmp_path)
    root = work_dir(tmp_path, work.id)
    assert sum(1 for _ in (root / "units").glob("*/*.json")) == PROTESTANT_CANON_CHAPTERS
    psalm = json.loads((root / "units" / "PSA" / "003.json").read_text())
    assert any(v["text"].endswith("Selah.") for v in psalm["verses"])
    assert not any("\\" in v["text"] for v in psalm["verses"])

"""Pointers to the texts this project will import — and only pointers.

No scripture is in this file or anywhere in the repository (`/CLAUDE.md`, rule 3).
A row here is a URL, a title and a licence stated in one sentence; the text itself
arrives with `videomaker library import <id>` and lives in the user's own
`workspace/library/`, which `.gitignore` already excludes.

Every row is an `ImportSpec`, so the licence gate that guards a hand-typed import
guards these too: a row cannot be added without its licence, its licence URL and
its source URL, because the model refuses to exist without them.
"""

from videomaker.corpus.importer import ImportSpec

#: The blessed texts, keyed by work id. ebible.org has no site-wide licence — each
#: translation's own details page is the authority, so `licence_url` points there
#: and not at the site root.
CATALOGUE: dict[str, ImportSpec] = {
    "web": ImportSpec(
        work_id="web",
        title="World English Bible",
        language="en",
        licence="Public domain.",
        licence_url="https://ebible.org/find/details.php?id=engwebp",
        source_url="https://ebible.org/",
        archive="https://ebible.org/Scriptures/engwebp_usfm.zip",
    ),
    "bsb": ImportSpec(
        work_id="bsb",
        title="Berean Standard Bible",
        language="en",
        licence="CC0: dedicated to the public domain on 2023-04-30.",
        licence_url="https://berean.bible/licensing.htm",
        source_url="https://berean.bible/",
        archive="https://bereanbible.com/bsb_usfm.zip",
    ),
    # The two hand-written book files under `tests/fixtures/usfm/`. Listed because
    # it is text the repository ships, and everything the repository ships states
    # its licence here; the acceptance script imports it with `--from`.
    "fixture": ImportSpec(
        work_id="fixture",
        title="Longhand Test Fixture",
        language="en",
        licence="AGPL-3.0-only, like the rest of this repository; invented text, nobody's scripture.",
        licence_url="https://www.gnu.org/licenses/agpl-3.0.html",
        source_url="https://github.com/kclaudeeager/ai-video-maker/tree/main/tests/fixtures/usfm",
    ),
    # Not yet enabled, with the reason — kept as prose rather than deleted so the
    # next person does not re-research them:
    #
    # "rv1909": Reina-Valera 1909, es, public domain. Needs a USFM source whose
    #   details page states the licence; ebible's `sparv1909` is the candidate.
    # "lsg": Louis Segond 1910, fr, public domain. Same: ebible `fraLSG`.
    # "kjv": King James Version, en. Public domain in the US and internationally,
    #   but UK printing sits under perpetual Crown letters patent — carry that note
    #   in `licence` if this is ever enabled.
}

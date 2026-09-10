# USFM fixtures

Two hand-written book files in USFM 3 for the importer and reader tests. The
words are invented for this repository and are nobody's scripture — the project
ships no scripture text (`/CLAUDE.md`, rule 3). The book codes are real USFM
codes so the web layer's `BOOK_ORDER` check accepts them.

`44-JHN.usfm` carries a section heading (`\s1`), a footnote (`\f ... \f*`), a
cross-reference (`\x ... \x*`) and a character marker (`\nd ... \nd*`), so the
tests can assert that none of the first three reaches `verses[].text` while the
wrapped word of the fourth does.

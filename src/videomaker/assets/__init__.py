"""Files that are neither code nor templates: the bundled fonts.

They live *inside* the package rather than at the repo root so that
`[tool.hatch.build.targets.wheel] packages = ["src/videomaker"]` carries them
into the wheel — the same reason `web/templates` and `web/static` are packaged
rather than kept beside them. Anchoring on `__file__` (not the CWD) is what makes
an installed copy work with no source tree present.

Two consumers, one copy:

* the review UI, which mounts `ASSETS_DIR` at `/assets` and `@font-face`s the
  files from `static/style.css` — no CDN, no build step, nothing fetched at
  runtime (M2 global constraint);
* thumbnail rendering (M3 Task 15), which draws with **Space Grotesk Bold** and
  should read it from `FONTS_DIR` rather than bundling a second family.

The files are redistributed byte-for-byte as released, under SIL OFL 1.1; see
`fonts/OFL.txt` and `NOTICE.md`. Charis SIL carries Reserved Font Names, so a
subsetted copy would be a Modified Version that may not keep its own name — which
is why nothing here is subsetted, and why the byte cost is accepted instead.
"""

from pathlib import Path

ASSETS_DIR = Path(__file__).resolve().parent
FONTS_DIR = ASSETS_DIR / "fonts"

#: The display face. Task 15 draws thumbnail text with this one.
DISPLAY_FONT = FONTS_DIR / "SpaceGrotesk-Bold.ttf"

__all__ = ["ASSETS_DIR", "DISPLAY_FONT", "FONTS_DIR"]

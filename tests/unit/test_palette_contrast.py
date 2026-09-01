"""The palette's contrast ratios, measured from the stylesheet itself.

`docs/ui-design.md` §10 promises "contrast is measured, in both themes, for text
and for control borders". Until now that promise was kept by hand and recorded in a
comment beside each token — which is exactly the kind of claim that rots the first
time someone retunes a colour and does not redo the arithmetic.

So the numbers are read out of `style.css` and checked here. Retuning the palette is
still entirely the owner's call (the token block says so); what this stops is
retuning it *below the floor* without noticing.

The floors are WCAG 2.1 AA: 4.5:1 for body text, 3:1 for the border of a control you
have to be able to find. `--warn` is checked against the ground as well as paper
because it is the one colour the whole design depends on being seen — it is the only
hue on an otherwise achromatic page, and it means a person is needed.
"""

import re
from pathlib import Path

import pytest

STYLESHEET = Path(__file__).resolve().parents[2] / "src/videomaker/web/static/style.css"

#: Text must clear this against whatever it is drawn on.
AA_TEXT = 4.5
#: A control border only has to be findable, not readable.
AA_NON_TEXT = 3.0


def _channel(value: float) -> float:
    value /= 255
    return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4


def luminance(hex_colour: str) -> float:
    raw = hex_colour.lstrip("#")
    r, g, b = (int(raw[i : i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * _channel(r) + 0.7152 * _channel(g) + 0.0722 * _channel(b)


def contrast(one: str, two: str) -> float:
    first, second = luminance(one), luminance(two)
    high, low = max(first, second), min(first, second)
    return (high + 0.05) / (low + 0.05)


def _tokens(block: str) -> dict[str, str]:
    return dict(re.findall(r"--([a-z-]+):\s*(#[0-9a-fA-F]{6})\s*;", block))


def palettes() -> dict[str, dict[str, str]]:
    """The light tokens from `:root`, and the dark ones from the media query.

    Split on the media query rather than parsed as CSS: the file is one hand-written
    stylesheet with exactly one `prefers-color-scheme` block, and a real parser would
    be a dependency bought to read two dozen hex values.
    """
    css = STYLESHEET.read_text()
    head, _, tail = css.partition("@media (prefers-color-scheme: dark)")
    assert tail, "the dark theme block has moved or been renamed"
    return {"light": _tokens(head), "dark": _tokens(tail.split("}\n}", 1)[0])}


THEMES = palettes()

#: Every token that is drawn as text, against every surface it is drawn on.
TEXT_ON = ["paper", "ground"]
TEXT_TOKENS = ["ink", "ink-soft", "accent", "ok", "warn", "bad"]


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("surface", TEXT_ON)
@pytest.mark.parametrize("token", TEXT_TOKENS)
def test_text_clears_aa_on_every_surface(theme, surface, token):
    palette = THEMES[theme]
    ratio = contrast(palette[token], palette[surface])

    assert ratio >= AA_TEXT, (
        f"{theme} --{token} on --{surface} is {ratio:.2f}:1, below AA's {AA_TEXT}:1"
    )


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_a_control_border_is_findable_against_the_page(theme):
    palette = THEMES[theme]
    ratio = contrast(palette["rule-field"], palette["ground"])

    assert ratio >= AA_NON_TEXT, f"{theme} --rule-field is {ratio:.2f}:1 on the ground"


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_filled_buttons_are_readable(theme):
    """`--accent` is near-black in light and near-white in dark, so this pair is the
    one that breaks first if either is nudged toward the middle."""
    palette = THEMES[theme]
    ratio = contrast(palette["on-accent"], palette["accent"])

    assert ratio >= AA_TEXT, f"{theme} --on-accent on --accent is {ratio:.2f}:1"


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_a_card_reads_as_raised_off_the_page(theme):
    """Paper and ground must not be the same value.

    The first pass had white cards on a near-white ground, which is why the page
    read as one flat sheet with rounded rectangles drawn on it. This is a floor, not
    a target — the lift is meant to be quiet.
    """
    palette = THEMES[theme]
    ratio = contrast(palette["paper"], palette["ground"])

    assert ratio > 1.05, f"{theme} paper and ground are {ratio:.2f}:1 — indistinguishable"


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_the_chrome_carries_no_hue(theme):
    """The design rule, asserted rather than described.

    `docs/ui-design.md` §3: everything the machine does is achromatic, so the one
    warm thing on a page is unmissable. An accent with real saturation in it — the
    indigo this replaced — competes with `--warn` and the rule quietly stops being
    true. Greys are allowed a point or two of drift; a hue is not.
    """
    for token in ("ground", "paper", "sunk", "ink", "ink-soft", "accent", "rule", "chrome"):
        raw = THEMES[theme][token].lstrip("#")
        r, g, b = (int(raw[i : i + 2], 16) for i in (0, 2, 4))
        spread = max(r, g, b) - min(r, g, b)

        assert spread <= 8, (
            f"{theme} --{token} ({THEMES[theme][token]}) has a {spread}-point colour "
            "spread; the chrome is meant to be grey"
        )


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_warn_is_the_one_token_that_is_actually_warm(theme):
    """...and it has to be, or the rule above has nothing to point at."""
    raw = THEMES[theme]["warn"].lstrip("#")
    r, _g, b = (int(raw[i : i + 2], 16) for i in (0, 2, 4))

    assert r > b, f"{theme} --warn is not warm: {THEMES[theme]['warn']}"
    assert (r - b) >= 40, f"{theme} --warn is too close to grey to read as a signal"

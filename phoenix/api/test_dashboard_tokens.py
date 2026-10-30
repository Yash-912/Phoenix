"""The look is swappable (styles/tokens.css), so the contrast it must keep is a test, not a hope.

Any colour pair the page draws text with has to stay readable (WCAG AA, 4.5:1) after
someone changes the tokens, and the two component stylesheets must not carry colours of
their own, or a token swap would only half apply.
"""

import re
from pathlib import Path

STYLES = Path(__file__).parent / "dashboard" / "styles"


def tokens() -> dict[str, str]:
    text = (STYLES / "tokens.css").read_text(encoding="utf-8")
    return dict(re.findall(r"--([a-z0-9-]+):\s*(#[0-9a-fA-F]{6})\s*;", text))


def luminance(hex_color: str) -> float:
    channels = [int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast(a: str, b: str) -> float:
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


TEXT_ON = {
    "ink": ["paper", "paper-raised", "paper-sunk"],
    "ink-muted": ["paper", "paper-raised", "paper-sunk"],
    "ink-faint": ["paper", "paper-raised"],
    "ink-on-solid": ["ink", "ink-muted"],
    # the diff view colours text, not just a dot
    "tone-good": ["paper-raised"],
    "tone-bad": ["paper-raised"],
}


def test_every_text_pair_the_page_draws_meets_aa():
    t = tokens()
    failures = [
        f"{fg} on {bg}: {contrast(t[fg], t[bg]):.2f}"
        for fg, backgrounds in TEXT_ON.items() for bg in backgrounds if contrast(t[fg], t[bg]) < 4.5
    ]
    assert not failures, failures


def test_the_stylesheets_beyond_tokens_hold_no_colours_of_their_own():
    for name in ("base.css", "components.css"):
        css = (STYLES / name).read_text(encoding="utf-8")
        stripped = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
        assert not re.search(r"#[0-9a-fA-F]{3,8}\b", stripped), f"{name} has a literal colour"
        assert not re.search(r"\brgba?\(", stripped), f"{name} has a literal colour"

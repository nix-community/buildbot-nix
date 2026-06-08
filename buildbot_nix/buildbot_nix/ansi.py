"""ANSI escape handling shared by the log renderer and failure
excerpts."""

from __future__ import annotations

import re

# One token per escape sequence. Colons in SGR: ITU T.416 syntax for
# extended colors (38:5:185), used by systemd among others. OSC is
# BEL- or ST-terminated; charset selects and other two-character
# escapes are matched so their final byte does not leak as text.
ANSI_TOKEN_RE = re.compile(
    r"\x1b\[(?P<sgr>[0-9;:]*)m"
    r"|\x1b\](?P<osc>[^\x07\x1b]*)(?:\x07|\x1b\\)"
    # Ordered after SGR: a plain [0-9;:]*m parameter list is colors,
    # anything else (private markers like ?<=>, intermediate bytes like
    # the space in DECSCUSR \x1b[0 q) falls through here.
    r"|\x1b\[[0-9;:?<=>]*[ -/]*[@-~]"
    r"|\x1b[()*+]."
    r"|\x1b[^\[\]]"
)
# C0 controls that mean nothing in a log (tab/newline/CR stay).
CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1a\x1c-\x1f\x7f]")
# An escape sequence cut off at the end of a chunk; OSC is held back
# until its terminator arrives, as are charset selects (\x1b( without
# the final byte) and CSI parameter/intermediate prefixes.
ANSI_PARTIAL_RE = re.compile(
    r"\x1b(\[[0-9;:?<=>]*[ -/]*|\][^\x07\x1b]*\x1b?|[()*+])?\Z"
)


def strip_ansi(text: str) -> str:
    """Plain-text consumers (curl, scripts, agents) want clean text;
    the HTML viewer renders the colored original."""
    return CTRL_RE.sub("", ANSI_TOKEN_RE.sub("", text))

"""Redaction for anything the dashboard shows a person.

The graph is meant to redact before a prompt is built; this is a second, separate
pass at the edge, because the API reads rows the graph wrote and a person is about
to read them. It lowers the risk of a credential reaching a screen. It does not
remove it, and a value in a shape these rules do not know will pass through.

Two kinds of rule. Key rules blank the value of a JSON field whose name says it
holds a secret, whatever the value looks like. Pattern rules rewrite text inside any
string: JWTs, cloud and vendor keys, bearer headers, key=value pairs, passwords in
connection strings, emails, and card numbers that pass a Luhn check.

The same value gets the same label within one Redactor (TOKEN_1 twice, never TOKEN_2),
so a reader can still see that two places hold the same thing. The label map lives only
as long as the Redactor, which the caller makes per request.
"""

import re
from collections import Counter

# A field is secret by name when its last word is one of these. The last word, not any
# word: tokens_spent and token_budget are counters, not credentials.
SECRET_KEY = re.compile(
    r"(^|[_\-\s])(password|passwd|pwd|secret|token|api_?key|apikey|authorization|cookie|set-cookie|private_?key|credentials?)$",
    re.IGNORECASE,
)

# Order matters: the specific shapes go before the general key=value rule so a JWT
# in a "token=" pair is counted once, as a JWT.
PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("connection_string", re.compile(r"\b([a-z][a-z0-9+.\-]*://[^\s:/@]+:)([^\s@/]+)(@)", re.IGNORECASE)),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b")),
    ("cloud_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("vendor_token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|xox[abprs]-[A-Za-z0-9\-]{10,}|xapp-[A-Za-z0-9\-]{10,}|sk-[A-Za-z0-9_\-]{20,})\b")),
    ("bearer", re.compile(r"(\bBearer\s+)([A-Za-z0-9._~+/\-]{8,}=*)", re.IGNORECASE)),
    (
        "secret_pair",
        re.compile(
            r"(\b(?:password|passwd|pwd|secret|token|api[_\-]?key|apikey|access[_\-]?key|auth)\b[\"']?\s*[=:]\s*[\"']?)([^\s\"',;&}]{3,})",
            re.IGNORECASE,
        ),
    ),
    ("email", re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")),
)

CARD = re.compile(r"\b(?:\d[ \-]?){13,19}\b")

MASK = "[REDACTED]"


def _luhn_ok(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        digit = int(char)
        if index % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


class Redactor:
    """One redaction pass's labels and counts. Make one per request."""

    def __init__(self) -> None:
        self.counts: Counter[str] = Counter()
        self._labels: dict[tuple[str, str], str] = {}
        self._seen_per_kind: Counter[str] = Counter()

    def _label(self, kind: str, value: str) -> str:
        key = (kind, value)
        label = self._labels.get(key)
        if label is None:
            self._seen_per_kind[kind] += 1
            label = f"[{kind.upper()}_{self._seen_per_kind[kind]}]"
            self._labels[key] = label
        self.counts[kind] += 1
        return label

    def text(self, value: str) -> str:
        if not value:
            return value
        out = value
        for kind, pattern in PATTERNS:
            if kind == "connection_string":
                out = pattern.sub(lambda m: m.group(1) + self._label(kind, m.group(2)) + m.group(3), out)
            elif kind in ("bearer", "secret_pair"):
                out = pattern.sub(
                    lambda m, k=kind: m.group(1) + (m.group(2) if m.group(2).startswith("[") else self._label(k, m.group(2))),
                    out,
                )
            else:
                out = pattern.sub(lambda m, k=kind: self._label(k, m.group(0)), out)
        return CARD.sub(self._card, out)

    def _card(self, match: re.Match[str]) -> str:
        digits = re.sub(r"\D", "", match.group(0))
        # Card numbers begin 2 to 6. Epoch timestamps (Loki sends them in nanoseconds, 19 digits
        # starting 17) pass the Luhn check one time in ten, and masking them blanks real log lines.
        if 13 <= len(digits) <= 19 and digits[0] in "23456" and _luhn_ok(digits):
            return self._label("card", digits)
        return match.group(0)

    def value(self, value):
        """Redact any JSON-shaped value, returning a copy and leaving the input alone."""
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            cleaned = {}
            for key, item in value.items():
                if isinstance(key, str) and SECRET_KEY.search(key) and item not in (None, "", [], {}):
                    self.counts["secret_field"] += 1
                    cleaned[key] = MASK
                else:
                    cleaned[key] = self.value(item)
            return cleaned
        if isinstance(value, (list, tuple)):
            return [self.value(item) for item in value]
        return value

    def summary(self) -> dict[str, int]:
        return dict(self.counts)

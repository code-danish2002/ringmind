"""Grounding guard: the bot may only say money amounts and calendar dates it has a source for.

Small open models sometimes invent account details ("your balance is $42.75") instead of calling
the billing tool. Before a sentence is spoken, every currency amount and month/day date in it is
checked against what this call actually knows: the call context, tool results, and what the
caller said. A sentence with an unsupported value is never spoken; the agent re-asks the model.
"""

import re

MONTHS = {m: i for i, m in enumerate(
    "january february march april may june july august september october november december".split(), 1)}
_MONTH_RE = "(" + "|".join(MONTHS) + "|" + "|".join(m[:3] for m in MONTHS) + r")\.?"
_DAY_RE = r"(\d{1,2})(?:st|nd|rd|th)?"

_MONEY = re.compile(r"\$\s?(\d[\d,]*(?:\.\d{1,2})?)")
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
_MONTH_DAY = re.compile(_MONTH_RE + r"\s+" + _DAY_RE + r"\b", re.I)
_DAY_MONTH = re.compile(r"\b" + _DAY_RE + r"\s+(?:of\s+)?" + _MONTH_RE, re.I)
_ISO = re.compile(r"\b\d{4}-(\d{2})-(\d{2})\b")


def _month(name: str) -> int:
    name = name.lower().rstrip(".")
    return MONTHS.get(name) or next(i for m, i in MONTHS.items() if m.startswith(name))


def money_amounts(text: str) -> set[float]:
    return {round(float(m.replace(",", "")), 2) for m in _MONEY.findall(text)}


def numbers(text: str) -> set[float]:
    out = set()
    for m in _NUMBER.findall(text):
        try:
            out.add(round(float(m.replace(",", "")), 2))
        except ValueError:
            pass
    return out


def month_days(text: str) -> set[tuple[int, int]]:
    found = {(_month(m), int(d)) for m, d in _MONTH_DAY.findall(text)}
    found |= {(_month(m), int(d)) for d, m in _DAY_MONTH.findall(text)}
    found |= {(int(m), int(d)) for m, d in _ISO.findall(text)}
    return {(m, d) for m, d in found if 1 <= d <= 31}


class Grounding:
    def __init__(self):
        self._numbers: set[float] = {0.0}
        self._dates: set[tuple[int, int]] = set()

    def add(self, text: str) -> None:
        """Record trusted text: call context, tool results, caller utterances."""
        self._numbers |= numbers(text)
        self._dates |= month_days(text)

    def unsupported(self, sentence: str) -> str | None:
        """Describe the first unsupported amount/date in `sentence`, or None if it's all sourced."""
        for amount in money_amounts(sentence):
            if amount not in self._numbers:
                return f"the amount ${amount:.2f}"
        for m, d in month_days(sentence):
            if (m, d) not in self._dates:
                return f"the date {list(MONTHS)[m - 1].title()} {d}"
        return None

"""Deterministic answer checks that need no model call.

Every rate, basis-point change, date and month in an answer must appear in a
document the answer cites. These checks run before any LLM judge: they are
free, exact and reproducible, so a failure here skips the paid judge entirely.

Known limits: bare numbers without a unit (index levels, prices in 원) are not
extracted yet, and bp/%p values are compared by magnitude, so the direction
(인상/인하) is left to the judge.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Document:
    """An evidence document: the id the answer cites, and the text facts are checked against."""

    document_id: str
    text: str


# A bracketed ISO date such as [2026-08-27] is a date, not a citation.
CITATION_PATTERN = re.compile(r"\[(?!20\d{2}-\d{2}-\d{2}\])([A-Za-z0-9_\-]+)\]")
# Longer units first so "0.25%p" is not read as "0.25%".
QUANTITY_PATTERN = re.compile(
    r"(-?\d+(?:\.\d+)?)\s*(퍼센트포인트|%포인트|%p|베이시스포인트|bp|퍼센트|%)"
)
KOREAN_DATE_PATTERN = re.compile(
    r"(?:(20\d{2})\s*년\s*)?(1[0-2]|0?[1-9])\s*월(?:\s*(3[01]|[12]\d|0?[1-9])\s*일)?"
)
ISO_DATE_PATTERN = re.compile(r"(20\d{2})-(\d{2})-(\d{2})")
REFUSAL_TEXT = "제공된 자료에서 확인할 수 없습니다"

_POINT_UNITS = frozenset({"퍼센트포인트", "%포인트", "%p"})
_BASIS_POINT_UNITS = frozenset({"베이시스포인트", "bp"})


@dataclass(frozen=True)
class CheckResult:
    passed: bool
    reasons: tuple[str, ...]
    unsupported_facts: tuple[str, ...]
    citations: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "reasons": list(self.reasons),
            "unsupported_facts": list(self.unsupported_facts),
            "citations": list(self.citations),
        }


def _date_facts(year: str, month: str, day: str) -> set[str]:
    """A date implies every coarser form, so '2026-08-27' also supports '8월' and '8월 27일'."""
    mm = f"{int(month):02d}"
    facts = {f"월:{mm}"}
    if year:
        facts.add(f"월:{year}-{mm}")
    if day:
        dd = f"{int(day):02d}"
        facts.add(f"일:{mm}-{dd}")
        if year:
            facts.add(f"일:{year}-{mm}-{dd}")
    return facts


def extract_facts(text: str) -> set[str]:
    """Normalize so '0.25%p' == '25bp', '3%' == '3.00%' and '8월 27일' matches '2026-08-27'.

    A year or day written in the answer must match too: '2025년 8월 27일' is not
    supported by a 2026-08-27 document.
    """
    facts: set[str] = set()
    for raw_value, unit in QUANTITY_PATTERN.findall(text):
        value = float(raw_value)
        if unit in _POINT_UNITS:
            facts.add(f"{abs(round(value * 100))}bp")
        elif unit in _BASIS_POINT_UNITS:
            facts.add(f"{abs(round(value))}bp")
        else:
            facts.add(f"{value:.2f}%")
    for year, month, day in KOREAN_DATE_PATTERN.findall(text):
        facts |= _date_facts(year, month, day)
    for year, month, day in ISO_DATE_PATTERN.findall(text):
        facts |= _date_facts(year, month, day)
    return facts


def check_answer(answer: str, documents: Sequence[Document]) -> CheckResult:
    """Check citations and facts against the documents the answer cites (not all retrieved)."""
    known = {document.document_id: document for document in documents}
    citations = tuple(dict.fromkeys(CITATION_PATTERN.findall(answer)))
    reasons: list[str] = []
    if not citations and REFUSAL_TEXT not in answer:
        reasons.append("no_citation")
    if any(citation not in known for citation in citations):
        reasons.append("unknown_citation")

    # Strip citations first: IDs such as evt_20260827_01 contain digits that are not facts.
    body = CITATION_PATTERN.sub(" ", answer)
    evidence = "\n".join(known[citation].text for citation in citations if citation in known)
    unsupported = tuple(sorted(extract_facts(body) - extract_facts(evidence)))
    if unsupported:
        reasons.append("unsupported_fact")
    return CheckResult(
        passed=not reasons,
        reasons=tuple(reasons),
        unsupported_facts=unsupported,
        citations=citations,
    )

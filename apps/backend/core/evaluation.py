"""Retrieval metrics over a golden question set (Recall@k, MRR)."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

VALID_QUESTION_TYPES = frozenset({"numeric", "context"})


class GoldenSetError(Exception):
    """Raised when the golden question file is malformed."""


@dataclass(frozen=True)
class GoldenCase:
    case_id: str
    question: str
    question_type: str
    relevant_ids: frozenset[str]


@dataclass(frozen=True)
class RetrievalScore:
    cases: int
    recall_at_k: float
    mrr: float
    k: int
    misses: tuple[str, ...]


def load_golden_set(path: Path) -> tuple[GoldenCase, ...]:
    """Load a JSONL golden set; each line needs id, question, type and relevant_ids."""
    cases: list[GoldenCase] = []
    seen: set[str] = set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            case = GoldenCase(
                case_id=str(row["id"]),
                question=str(row["question"]),
                question_type=str(row["type"]),
                relevant_ids=frozenset(map(str, row["relevant_ids"])),
            )
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise GoldenSetError(f"{path.name}:{line_number} 형식 오류") from exc
        if case.question_type not in VALID_QUESTION_TYPES:
            raise GoldenSetError(f"{path.name}:{line_number} 알 수 없는 type: {case.question_type}")
        if not case.relevant_ids:
            raise GoldenSetError(f"{path.name}:{line_number} relevant_ids가 비어 있습니다.")
        if case.case_id in seen:
            raise GoldenSetError(f"{path.name}:{line_number} 중복 id: {case.case_id}")
        seen.add(case.case_id)
        cases.append(case)
    return tuple(cases)


def recall_at_k(ranked_ids: Sequence[str], relevant_ids: frozenset[str], k: int) -> float:
    """Fraction of relevant documents that appear in the top k."""
    if not relevant_ids:
        return 0.0
    return len(relevant_ids & set(ranked_ids[:k])) / len(relevant_ids)


def reciprocal_rank(ranked_ids: Sequence[str], relevant_ids: frozenset[str]) -> float:
    """1 / rank of the first relevant document, or 0 when none is retrieved."""
    for rank, document_id in enumerate(ranked_ids, start=1):
        if document_id in relevant_ids:
            return 1.0 / rank
    return 0.0


def score_retrieval(
    cases: Sequence[GoldenCase],
    rank: Callable[[str], Sequence[str]],
    k: int = 5,
) -> RetrievalScore:
    """Run ``rank`` for every case and average Recall@k and MRR."""
    if not cases:
        raise GoldenSetError("평가할 질문이 없습니다.")
    recalls: list[float] = []
    reciprocal_ranks: list[float] = []
    misses: list[str] = []
    for case in cases:
        ranked_ids = list(rank(case.question))
        recall = recall_at_k(ranked_ids, case.relevant_ids, k)
        recalls.append(recall)
        reciprocal_ranks.append(reciprocal_rank(ranked_ids, case.relevant_ids))
        if recall < 1.0:
            misses.append(case.case_id)
    return RetrievalScore(
        cases=len(cases),
        recall_at_k=sum(recalls) / len(cases),
        mrr=sum(reciprocal_ranks) / len(cases),
        k=k,
        misses=tuple(misses),
    )

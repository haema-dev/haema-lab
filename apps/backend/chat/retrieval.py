"""pgvector 검색: 근거 청크(source_chunk)와 답변 캐시(answer_cache).

두 테이블은 따로 검색한다. 생성된 답(answer_cache)은 근거로 쓰지 않는다.
"""

from collections.abc import Sequence
from dataclasses import replace

from django.conf import settings
from pgvector.django import CosineDistance

from chat.models import AnswerCache, SourceChunk
from core.verify import Document

DEFAULT_TOP_K = 4
DEFAULT_CONTEXT_MAX_CHARS = 2_000  # CLAUDE.md 측정된 제약: 근거 문맥 2,000자


def search_chunks(
    vector: Sequence[float], top_k: int | None = None, min_similarity: float | None = None
) -> list[Document]:
    """가장 가까운 근거 청크를 가까운 순서로. 인용 id 는 청크 id(문자열)다.

    min_similarity=None 이면 거르지 않는다. 임계값은 골든셋으로 보정한 뒤에 켠다.
    """
    top_k = getattr(settings, "RAG_TOP_K", DEFAULT_TOP_K) if top_k is None else top_k
    rows = (
        SourceChunk.objects.filter(embedding__isnull=False)
        .annotate(distance=CosineDistance("embedding", list(vector)))
        .order_by("distance")[:top_k]
    )
    return [
        Document(document_id=str(row.id), text=row.content)
        for row in rows
        if min_similarity is None or (1 - row.distance) >= min_similarity
    ]


def find_exact_cache(question: str) -> AnswerCache | None:
    """질문 문자열이 같은(앞뒤 공백 제외) 검증된 답변."""
    return AnswerCache.objects.filter(question=question.strip()).order_by("-updated_at").first()


def find_similar_cache(vector: Sequence[float], min_similarity: float | None) -> AnswerCache | None:
    """켜져 있고(임계값 지정) 충분히 가까운 캐시 답변만. 기본은 꺼짐."""
    if min_similarity is None:
        return None
    row = (
        AnswerCache.objects.filter(question_embedding__isnull=False)
        .annotate(distance=CosineDistance("question_embedding", list(vector)))
        .order_by("distance")
        .first()
    )
    if row is not None and (1 - row.distance) >= min_similarity:
        return row
    return None


def invalidate_cache_for_chunks(chunk_ids: Sequence[int]) -> int:
    """이 청크들 중 하나라도 인용한 캐시 답변을 지운다(근거가 바뀌었으니 다시 생성해야 한다). 지운 수를 돌려준다."""
    if not chunk_ids:
        return 0
    deleted, _ = AnswerCache.objects.filter(cited_chunk_ids__overlap=list(chunk_ids)).delete()
    return deleted


def fit_context(documents: Sequence[Document], budget: int | None = None) -> tuple[Document, ...]:
    """순위 순서로 문서를 통째로 담되 예산을 넘으면 멈춘다. 1위 문서만 예산보다 길 때 자른다."""
    budget = getattr(settings, "CONTEXT_MAX_CHARS", DEFAULT_CONTEXT_MAX_CHARS) if budget is None else budget
    selected: list[Document] = []
    used = 0
    for document in documents:
        if used + len(document.text) <= budget:
            selected.append(document)
            used += len(document.text)
        elif not selected:
            selected.append(replace(document, text=document.text[:budget]))
            used = budget
    return tuple(selected)

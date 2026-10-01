"""Every DB read and write of the question flow. Only Django touches the DB.

Writes happen at four points only (numbers follow the architecture doc):

    3   message(role=user) + job(queued)                       -> create_question_job
    7   job queued -> running                                   -> mark_job_running
    19  answer + answer_citation + message + job(succeeded)     -> save_new_answer / save_reused_answer
    20  job(failed)                                             -> mark_job_failed

Between 7 and 19/20 the worker only reads, in autocommit mode (no transaction),
so a slow model call never holds a transaction open.
"""

from __future__ import annotations

import hashlib
import unicodedata
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from pgvector.django import CosineDistance

from qa.models import Answer, AnswerCitation, DocumentChunk, Job, Message, SourceDocument, citation_key


class JobNotRunningError(Exception):
    """The job left 'running' (e.g. reaped as stale) before the worker saved its result."""


@dataclass(frozen=True)
class Evidence:
    """One retrieved chunk as the worker uses it (core.verify.Document)."""

    chunk_id: int
    document_id: str  # citation key, e.g. "c42"
    text: str


# ---------------------------------------------------------------------------
# Question normalization (2. duplicate check)
# ---------------------------------------------------------------------------


def normalize_question(question: str) -> str:
    """NFKC, collapse whitespace, lowercase. '  금리  인상?' == '금리 인상?'"""
    return " ".join(unicodedata.normalize("NFKC", question).split()).lower()


def question_hash(question: str) -> str:
    return hashlib.sha256(normalize_question(question).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# API side: 2, 3 (write) and job status (read)
# ---------------------------------------------------------------------------


def find_active_job(user_id: int, qhash: str) -> Job | None:
    return (
        Job.objects.filter(user_id=user_id, question_hash=qhash, status__in=Job.ACTIVE_STATUSES)
        .only("id")
        .first()
    )


def create_question_job(user_id: int, question: str) -> tuple[uuid.UUID, bool]:
    """Return (job_id, created). created=False means an active duplicate already exists."""
    qhash = question_hash(question)
    existing = find_active_job(user_id, qhash)
    if existing is not None:
        return existing.id, False
    try:
        with transaction.atomic():
            message = Message.objects.create(user_id=user_id, role=Message.Role.USER, content=question)
            job = Job.objects.create(user_id=user_id, question_hash=qhash, user_message=message)
    except IntegrityError:
        # Lost a race against the same request: the partial unique index
        # job_one_active_per_question rejected our row. Return the winner.
        existing = find_active_job(user_id, qhash)
        if existing is None:
            raise
        return existing.id, False
    return job.id, True


def get_job_view(job_id: uuid.UUID, user_id: int) -> dict[str, Any] | None:
    """What polling returns. None when the job does not exist or is another user's."""
    job = (
        Job.objects.select_related("answer")
        .filter(id=job_id, user_id=user_id)
        .first()
    )
    if job is None:
        return None
    view: dict[str, Any] = {
        "job_id": str(job.id),
        "status": job.status,
        "created_at": job.created_at.isoformat(),
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }
    if job.status == Job.Status.FAILED:
        view["failure_type"] = job.failure_type
    if job.status == Job.Status.SUCCEEDED and job.answer is not None:
        citations = (
            AnswerCitation.objects.filter(answer_id=job.answer_id)
            .select_related("chunk__document")
            .order_by("ordinal")
        )
        view["answer"] = {
            "text": job.answer.answer_text,
            "model": job.answer.model_name,
            "reused": job.reused_answer,
            "citations": [
                {
                    "key": citation_key(c.chunk_id),
                    "document": c.chunk.document.title or c.chunk.document.external_id,
                    "source_uri": c.chunk.document.source_uri,
                }
                for c in citations
            ],
        }
    return view


def count_user_failures(user_id: int) -> int:
    """User failure counter: verification failures only, never infra."""
    return Job.objects.filter(
        user_id=user_id, status=Job.Status.FAILED, failure_type=Job.FailureType.VERIFICATION
    ).count()


# ---------------------------------------------------------------------------
# Worker side
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunningJob:
    id: uuid.UUID
    user_id: int
    question: str


def mark_job_running(job_id: str | uuid.UUID) -> RunningJob | None:
    """7. queued -> running. None if the job is gone or already taken (popped twice, reaped)."""
    updated = Job.objects.filter(id=job_id, status=Job.Status.QUEUED).update(
        status=Job.Status.RUNNING, started_at=timezone.now()
    )
    if not updated:
        return None
    job = Job.objects.select_related("user_message").get(id=job_id)
    return RunningJob(id=job.id, user_id=job.user_id, question=job.user_message.content or "")


def find_similar_answer(question_vector: Sequence[float], max_distance: float) -> Answer | None:
    """10. Nearest earlier answer by question embedding.

    Answers that cite a retired chunk (the source document changed) are skipped.
    """
    return (
        Answer.objects.annotate(distance=CosineDistance("question_embedding", list(question_vector)))
        .filter(distance__lte=max_distance)
        .exclude(citations__chunk__retired_at__isnull=False)
        .order_by("distance")
        .only("id", "answer_text", "model_name")
        .first()
    )


def search_chunks(question_vector: Sequence[float], *, limit: int, max_chars: int) -> tuple[Evidence, ...]:
    """12. Up to ``limit`` nearest active chunks whose total length fits ``max_chars``.

    Chunks are taken in distance order; one that does not fit the remaining
    budget is skipped and a shorter, farther one may still fit. Only the top
    chunk is cut when it alone exceeds the budget (indexing keeps chunks at
    QA_CHUNK_MAX_CHARS, so this is a guard, not the normal path).
    """
    candidates = (
        DocumentChunk.objects.filter(retired_at__isnull=True)
        .annotate(distance=CosineDistance("embedding", list(question_vector)))
        .order_by("distance")
        .only("id", "content")[:limit]
    )
    selected: list[Evidence] = []
    remaining = max_chars
    for chunk in candidates:
        text = chunk.content
        if not selected and len(text) > remaining:
            text = text[:remaining]
        if not text or len(text) > remaining:
            continue
        selected.append(Evidence(chunk_id=chunk.id, document_id=chunk.citation_key, text=text))
        remaining -= len(text)
    return tuple(selected)


def save_new_answer(
    job_id: uuid.UUID,
    *,
    user_id: int,
    answer_text: str,
    model_name: str,
    is_fallback: bool,
    verification: dict[str, Any],
    question_vector: Sequence[float],
    embedding_model: str,
    cited_chunk_ids: Sequence[int],
    infra_retries: int,
) -> None:
    """19-A. One transaction: answer + answer_citation + message(assistant) + job(succeeded)."""
    with transaction.atomic():
        answer = Answer.objects.create(
            answer_text=answer_text,
            model_name=model_name,
            is_fallback=is_fallback,
            verification=verification,
            question_embedding=list(question_vector),
            embedding_model=embedding_model,
        )
        AnswerCitation.objects.bulk_create(
            AnswerCitation(answer=answer, chunk_id=chunk_id, ordinal=ordinal)
            for ordinal, chunk_id in enumerate(cited_chunk_ids)
        )
        _finish_success(job_id, user_id=user_id, answer_id=answer.id, reused=False, infra_retries=infra_retries)


def save_reused_answer(job_id: uuid.UUID, *, user_id: int, answer_id: int, infra_retries: int) -> None:
    """19-B. One transaction: message(assistant -> existing answer) + job(succeeded). No new vector."""
    with transaction.atomic():
        _finish_success(job_id, user_id=user_id, answer_id=answer_id, reused=True, infra_retries=infra_retries)


def _finish_success(job_id: uuid.UUID, *, user_id: int, answer_id: int, reused: bool, infra_retries: int) -> None:
    message = Message.objects.create(user_id=user_id, role=Message.Role.ASSISTANT, answer_id=answer_id)
    updated = Job.objects.filter(id=job_id, status=Job.Status.RUNNING).update(
        status=Job.Status.SUCCEEDED,
        assistant_message=message,
        answer_id=answer_id,
        reused_answer=reused,
        infra_retries=infra_retries,
        finished_at=timezone.now(),
    )
    if not updated:
        raise JobNotRunningError(str(job_id))  # rolls back the whole transaction


def mark_job_failed(
    job_id: uuid.UUID | str,
    *,
    failure_type: str,
    detail: str = "",
    infra_retries: int = 0,
    from_statuses: Sequence[str] = (Job.Status.RUNNING,),
) -> bool:
    """20. Only the job row changes; no answer, no question vector."""
    return bool(
        Job.objects.filter(id=job_id, status__in=from_statuses).update(
            status=Job.Status.FAILED,
            failure_type=failure_type,
            failure_detail=detail[:1000],
            infra_retries=infra_retries,
            finished_at=timezone.now(),
        )
    )


def reap_stale_jobs(*, running_seconds: int, queued_seconds: int) -> list[uuid.UUID]:
    """Fail jobs a dead worker or a lost queue item left behind.

    Without this an orphaned active job would block the same question forever
    (job_one_active_per_question). Counted as infra, not as the user's failure.
    """
    now = timezone.now()
    stale = Q(status=Job.Status.RUNNING, started_at__lt=now - timedelta(seconds=running_seconds)) | Q(
        status=Job.Status.QUEUED, created_at__lt=now - timedelta(seconds=queued_seconds)
    )
    with transaction.atomic():
        ids = list(Job.objects.select_for_update(skip_locked=True).filter(stale).values_list("id", flat=True))
        Job.objects.filter(id__in=ids).update(
            status=Job.Status.FAILED,
            failure_type=Job.FailureType.INFRA,
            failure_detail="stale: worker or queue lost the job",
            finished_at=now,
        )
    return ids


# ---------------------------------------------------------------------------
# Indexing side (0-3)
# ---------------------------------------------------------------------------


def get_document_hash(external_id: str) -> str | None:
    return SourceDocument.objects.filter(external_id=external_id).values_list("content_sha256", flat=True).first()


def save_document(
    *,
    external_id: str,
    title: str,
    source_uri: str,
    content_sha256: str,
    chunks: Sequence[tuple[str, Sequence[float]]],
    embedding_model: str,
) -> int:
    """0-3. One transaction: source_document + its chunks. Old chunks are retired, not deleted."""
    now = timezone.now()
    with transaction.atomic():
        document, _ = SourceDocument.objects.update_or_create(
            external_id=external_id,
            defaults={"title": title, "source_uri": source_uri, "content_sha256": content_sha256},
        )
        DocumentChunk.objects.filter(document=document, retired_at__isnull=True).update(retired_at=now)
        DocumentChunk.objects.bulk_create(
            DocumentChunk(
                document=document,
                chunk_index=index,
                content=content,
                embedding=list(vector),
                embedding_model=embedding_model,
            )
            for index, (content, vector) in enumerate(chunks)
        )
    return document.id

"""One job from start to finish (architecture doc steps 6-20).

    7  job running                     (DB write)
    9  FastAPI /embed kind=query       (GPU slot)   -> vector stays in memory
    10 similar answer?                 (DB read)    -> yes: 19-B
    12 document_chunk top 5 / 2,000 chars (DB read) -> none: 20 (infra, no_evidence)
    13 FastAPI /generate               (GPU slot)
    14 core/verify.py                  -> fail: 17
    15 FastAPI /judge (Gemini)         -> claims or error: 17, none: 19-A
    17 FastAPI /fallback (Gemini) + core/verify.py -> pass: 19-A, fail: 20
    19 success (one transaction) / 20 failure (job row only)
    -> after COMMIT: Redis "job finished" notice

Infra errors are retried inside each call (with_infra_retry); when retries run
out the job fails as 'infra', which never counts against the user.
Verification failures are not retried.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

import redis
from django.conf import settings

from core.verify import CheckResult, check_generation
from qa import repository
from qa.fastapi_client import (
    Context,
    FastAPIClient,
    FastAPIError,
    Generation,
    InfraError,
    RetryCounter,
    with_infra_retry,
)
from qa.models import Job
from qa.redis_queue import GpuBusyError, gpu_slot, notify_job_done

logger = logging.getLogger(__name__)

T = TypeVar("T")


class VerificationFailed(Exception):
    def __init__(self, verification: dict[str, Any]) -> None:
        super().__init__("answer failed verification")
        self.verification = verification


class NoEvidence(Exception):
    """document_chunk has no active chunk to answer from (nothing indexed yet).

    The gateway rejects empty contexts, and an answer without evidence cannot
    pass core/verify.py, so the job fails before any generation. It is a data
    problem on our side, so it is recorded as infra, not as the user's failure.
    """


@dataclass(frozen=True)
class _Accepted:
    generation: Generation
    check: CheckResult
    is_fallback: bool


def on_gpu(call: Callable[[], T]) -> Callable[[], T]:
    """Take the GPU slot per attempt, so a retry's back-off sleep does not hold it."""

    def run() -> T:
        try:
            with gpu_slot():
                return call()
        except GpuBusyError as exc:
            raise InfraError(str(exc)) from exc

    return run


def _check_dict(generation: Generation, check: CheckResult) -> dict[str, Any]:
    return {"model": generation.model, "done_reason": generation.done_reason, **check.as_dict()}


def process_job(job_id: str | uuid.UUID, client: FastAPIClient) -> str | None:
    """Run one job. Returns the final status, or None if the job was not ours to run."""
    running = repository.mark_job_running(job_id)
    if running is None:
        logger.info("job %s skipped: not queued", job_id)
        return None

    retries = RetryCounter()
    status = Job.Status.FAILED
    try:
        _answer(running, client, retries)
        status = Job.Status.SUCCEEDED
    except NoEvidence:
        logger.warning("job %s failed: no evidence indexed", job_id)
        repository.mark_job_failed(
            running.id, failure_type=Job.FailureType.INFRA, detail="no_evidence", infra_retries=retries.count
        )
    except VerificationFailed as exc:
        logger.info("job %s failed verification: %s", job_id, exc.verification)
        repository.mark_job_failed(
            running.id,
            failure_type=Job.FailureType.VERIFICATION,
            detail=str(exc.verification),
            infra_retries=retries.count,
        )
    except repository.JobNotRunningError:
        logger.warning("job %s was reaped while running; result discarded", job_id)
        return None
    except (FastAPIError, redis.RedisError) as exc:
        # FastAPIError covers InfraError (retries exhausted) and contract errors (4xx):
        # neither is the user's fault.
        logger.warning("job %s infra failure: %s", job_id, exc)
        repository.mark_job_failed(
            running.id, failure_type=Job.FailureType.INFRA, detail=str(exc), infra_retries=retries.count
        )
    except Exception as exc:
        logger.exception("job %s crashed", job_id)
        repository.mark_job_failed(
            running.id, failure_type=Job.FailureType.INFRA, detail=f"internal: {exc!r}", infra_retries=retries.count
        )

    # The DB commit is done; a lost notice only delays SSE, polling still sees it.
    try:
        notify_job_done(running.id, status)
    except redis.RedisError:
        logger.warning("job %s done notice failed", job_id)
    return status


def _answer(job: repository.RunningJob, client: FastAPIClient, retries: RetryCounter) -> None:
    # 9. question embedding: memory only until step 19-A.
    embedded = with_infra_retry(on_gpu(lambda: client.embed([job.question], kind="query")), counter=retries)
    question_vector = embedded.vectors[0]

    # 10-11. reuse a close earlier answer and skip everything else.
    similar = repository.find_similar_answer(question_vector, settings.QA_ANSWER_REUSE_MAX_DISTANCE)
    if similar is not None:
        repository.save_reused_answer(
            job.id, user_id=job.user_id, answer_id=similar.id, infra_retries=retries.count
        )
        return

    # 12. evidence.
    evidence = repository.search_chunks(
        question_vector, limit=settings.QA_CONTEXT_MAX_CHUNKS, max_chars=settings.QA_CONTEXT_MAX_CHARS
    )
    if not evidence:
        raise NoEvidence()
    contexts = [Context(id=e.document_id, text=e.text) for e in evidence]
    chunk_id_by_key = {e.document_id: e.chunk_id for e in evidence}

    accepted, verification = _generate_and_verify(job.question, contexts, evidence, client, retries)

    # 19-A.
    repository.save_new_answer(
        job.id,
        user_id=job.user_id,
        answer_text=accepted.generation.text,
        model_name=accepted.generation.model,
        is_fallback=accepted.is_fallback,
        verification=verification,
        question_vector=question_vector,
        embedding_model=embedded.model,
        cited_chunk_ids=[chunk_id_by_key[key] for key in accepted.check.citations],
        infra_retries=retries.count,
    )


def _generate_and_verify(
    question: str,
    contexts: list[Context],
    documents: tuple[repository.Evidence, ...],
    client: FastAPIClient,
    retries: RetryCounter,
) -> tuple[_Accepted, dict[str, Any]]:
    verification: dict[str, Any] = {}

    # 13. generate on the iGPU.
    generation = with_infra_retry(on_gpu(lambda: client.generate(question, contexts)), counter=retries)
    # 14. code check first; a failure skips the paid judge.
    check = check_generation(generation.text, documents, truncated=generation.truncated)
    verification["generate"] = _check_dict(generation, check)

    if check.passed:
        # 15. Gemini judge. An error after retries counts as a judge failure (16) -> fallback.
        try:
            judgement = with_infra_retry(lambda: client.judge(question, generation.text, contexts), counter=retries)
        except FastAPIError as exc:
            verification["judge"] = {"error": str(exc)}
        else:
            verification["judge"] = {"model": judgement.model, "unsupported_claims": judgement.unsupported_claims}
            if not judgement.unsupported_claims:
                return _Accepted(generation, check, is_fallback=False), verification

    # 17. fallback with the same evidence; must pass the same code check.
    fallback = with_infra_retry(lambda: client.fallback(question, contexts), counter=retries)
    fallback_check = check_generation(fallback.text, documents, truncated=fallback.truncated)
    verification["fallback"] = _check_dict(fallback, fallback_check)
    if not fallback_check.passed:
        raise VerificationFailed(verification)
    return _Accepted(fallback, fallback_check, is_fallback=True), verification

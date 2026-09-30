"""Monthly JSON -> retrieval documents -> idempotent vector index.

The JSON files stay the source of truth for numbers. The vector index is only a
semantic search aid, keyed by ``(document_id, embedding_model)`` and refreshed
when ``content_sha256`` changes, so re-running the sync embeds nothing new.
``InMemoryVectorIndex`` is the stand-in until the pgvector table is connected.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

MONTH_FILE_PATTERN = re.compile(r"^(\d{4})-(\d{2})\.json$")
# Bump whenever render_event output changes: the content hash includes it, so a
# persistent index re-embeds every document instead of keeping stale vectors.
RENDER_VERSION = 2

# Decision fields rendered into the embedding text, in display order.
_DECISION_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("change_basis_points", "변동", "bp"),
    ("previous_rate_percent", "이전 금리", "%"),
    ("target_rate_percent", "결정 후 금리", "%"),
    ("target_range_lower_percent", "목표 범위 하단", "%"),
    ("target_range_upper_percent", "목표 범위 상단", "%"),
)


class IngestError(Exception):
    """Raised when a monthly JSON file cannot be read."""


@dataclass(frozen=True)
class Document:
    document_id: str
    text: str
    month: str = ""
    content_sha256: str = ""


@dataclass(frozen=True)
class IndexedVector:
    document_id: str
    embedding_model: str
    content_sha256: str
    vector: tuple[float, ...]


@dataclass(frozen=True)
class IndexReport:
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0


class VectorIndex(Protocol):
    """Storage contract shared by the in-memory index and the future pgvector table."""

    def get(self, document_id: str, embedding_model: str) -> IndexedVector | None: ...

    def upsert(self, entries: Sequence[IndexedVector]) -> None: ...


class InMemoryVectorIndex:
    """Process-local index; thread-safe for Django's threaded dev server."""

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str], IndexedVector] = {}
        self._lock = threading.Lock()

    def get(self, document_id: str, embedding_model: str) -> IndexedVector | None:
        with self._lock:
            return self._entries.get((embedding_model, document_id))

    def upsert(self, entries: Sequence[IndexedVector]) -> None:
        with self._lock:
            for entry in entries:
                self._entries[(entry.embedding_model, entry.document_id)] = entry

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


def available_month_files(data_directory: Path) -> dict[str, Path]:
    """Map ``YYYY-MM`` to its JSON file for every monthly file in the directory."""
    files: dict[str, Path] = {}
    for path in data_directory.iterdir():
        match = MONTH_FILE_PATTERN.fullmatch(path.name)
        if path.is_file() and match:
            files[f"{match.group(1)}-{match.group(2)}"] = path
    return files


def content_hash(value: Any) -> str:
    """SHA-256 of canonical JSON, stable across key order and whitespace."""
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _format_number(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        return f"{value:.2f}"
    return str(value)


def _render_decision(decision: dict[str, Any]) -> str:
    parts = [
        str(decision[key])
        for key in ("policy_instrument", "action_ko")
        if decision.get(key) not in (None, "")
    ]
    for key, label, unit in _DECISION_FIELDS:
        value = decision.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            sign = "+" if key == "change_basis_points" and value > 0 else ""
            parts.append(f"{label} {sign}{_format_number(value)}{unit}")
    return ", ".join(parts)


def render_event(event: dict[str, Any], sources: Sequence[dict[str, Any]]) -> str:
    """Render one event as plain Korean text for embedding and prompt context.

    Readable text embeds better than raw JSON, which is dominated by keys and URLs.
    """
    # No brackets around the date: brackets are reserved for citation IDs in answers.
    header = " · ".join(
        str(part) for part in (event.get("event_date"), event.get("title")) if part
    )
    lines = [header] if header else []
    meta = [
        f"{label}: {', '.join(map(str, value)) if isinstance(value, list) else value}"
        for label, value in (
            ("분류", event.get("category")),
            ("국가", event.get("countries")),
            ("기관", event.get("entities")),
        )
        if value
    ]
    if meta:
        lines.append(" | ".join(meta))
    if event.get("summary"):
        lines.append(f"요약: {event['summary']}")
    decision = event.get("decision")
    if isinstance(decision, dict) and (rendered := _render_decision(decision)):
        lines.append(f"결정: {rendered}")
    for source in sources:
        described = " ".join(
            str(source[key]) for key in ("institution", "title") if source.get(key)
        )
        if source.get("published_at"):
            described += f" ({source['published_at']})"
        lines.append(f"출처: {described.strip()}")
    return "\n".join(lines)


def load_documents(path: Path) -> tuple[Document, ...]:
    """Turn each monthly event into a source-aware retrieval document."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IngestError(f"JSON 데이터를 읽을 수 없습니다: {path.name}") from exc
    if not isinstance(payload, dict):
        raise IngestError(f"JSON 최상위 구조가 객체가 아닙니다: {path.name}")

    month = str(payload.get("observation_month") or "")
    sources = {
        source["source_id"]: source
        for source in payload.get("sources", [])
        if isinstance(source, dict) and source.get("source_id")
    }
    documents: list[Document] = []
    for index, event in enumerate(payload.get("events", [])):
        if not isinstance(event, dict):
            continue
        linked = [sources[sid] for sid in event.get("source_ids", []) if sid in sources]
        documents.append(
            Document(
                document_id=str(event.get("event_id") or f"{month}-event-{index + 1}"),
                text=render_event(event, linked),
                month=month,
                content_sha256=content_hash(
                    {"event": event, "sources": linked, "render_version": RENDER_VERSION}
                ),
            )
        )
    return tuple(documents)


def load_all_documents(data_directory: Path) -> tuple[Document, ...]:
    """Load documents from every monthly file, oldest month first."""
    files = available_month_files(data_directory)
    return tuple(doc for month in sorted(files) for doc in load_documents(files[month]))


def sync_index(
    documents: Iterable[Document],
    index: VectorIndex,
    embed: Callable[[list[str]], list[list[float]]],
    embedding_model: str,
    batch_size: int = 16,
) -> IndexReport:
    """Embed only documents that are new or whose content hash changed.

    Running this twice over the same documents embeds nothing the second time.
    """
    pending: list[tuple[Document, bool]] = []
    unchanged = 0
    for document in documents:
        existing = index.get(document.document_id, embedding_model)
        if existing is not None and existing.content_sha256 == document.content_sha256:
            unchanged += 1
        else:
            pending.append((document, existing is not None))

    for start in range(0, len(pending), batch_size):
        batch = pending[start : start + batch_size]
        vectors = embed([document.text for document, _ in batch])
        if len(vectors) != len(batch):
            raise IngestError("임베딩 개수가 문서 개수와 다릅니다.")
        index.upsert(
            [
                IndexedVector(
                    document_id=document.document_id,
                    embedding_model=embedding_model,
                    content_sha256=document.content_sha256,
                    vector=tuple(vector),
                )
                for (document, _), vector in zip(batch, vectors, strict=True)
            ]
        )

    updated = sum(1 for _, existed in pending if existed)
    return IndexReport(inserted=len(pending) - updated, updated=updated, unchanged=unchanged)

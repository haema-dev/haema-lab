"""0. Batch indexing of source documents, unrelated to user requests.

    uv run python manage.py index_documents data/   # *.md, *.txt, *.json (official_events)

0-1 split each file into chunks (a JSON file becomes one paragraph per event); 0-2 embed them through FastAPI /embed
(kind=document) outside any transaction; 0-3 save source_document +
document_chunk in one transaction. Unchanged files (same sha256) are skipped.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from qa import repository
from qa.fastapi_client import FastAPIClient, with_infra_retry
from qa.worker import on_gpu

SUFFIXES = {".md", ".txt", ".json"}


def render_events_json(raw: str) -> str:
    """official_events JSON (data/YYYY-MM.json) -> text, one paragraph per event.

    Numbers are written the way core/verify.py reads them (2.75%, 25bp,
    2026-08-27), so an answer that copies them passes the numeric check.
    """
    data = json.loads(raw)
    if not isinstance(data, dict) or data.get("record_type") != "official_events":
        raise ValueError("not an official_events file")
    sources = {s.get("source_id"): s for s in data.get("sources") or [] if isinstance(s, dict)}
    paragraphs = []
    for event in data.get("events") or []:
        lines = [f"{event.get('event_date') or ''} {event.get('title') or ''}".strip()]
        if event.get("summary"):
            lines.append(event["summary"])
        decision = event.get("decision") or {}
        previous, target = decision.get("previous_rate_percent"), decision.get("target_rate_percent")
        lower, upper = decision.get("target_range_lower_percent"), decision.get("target_range_upper_percent")
        if previous is not None and target is not None:
            level = f"{previous:.2f}% → {target:.2f}%"
        elif lower is not None and upper is not None:
            # Both ends carry %: core/verify.py does not read a bare "3.75" in "3.75~4.00%".
            level = f"{lower:.2f}% ~ {upper:.2f}%"
        else:
            level = ""
        if level:
            change = decision.get("change_basis_points")
            detail = ", ".join(
                part for part in (decision.get("action_ko"), f"{abs(change)}bp" if change is not None else "") if part
            )
            lines.append(
                f"{decision.get('policy_instrument') or '정책금리'}: {level}" + (f" ({detail})" if detail else "")
            )
        # Institution only: a source URL is ~170 chars of prompt (~70 tok/s on the iGPU) and no fact.
        institutions = dict.fromkeys(
            sources[sid].get("institution") for sid in event.get("source_ids") or [] if sid in sources
        )
        if any(institutions):
            lines.append("출처: " + ", ".join(i for i in institutions if i))
        paragraphs.append("\n".join(lines))
    return "\n\n".join(paragraphs)


def split_into_chunks(text: str, max_chars: int) -> list[str]:
    """Pack paragraphs into chunks of at most ``max_chars``; a longer paragraph is cut hard."""
    chunks: list[str] = []
    current = ""
    for paragraph in (p.strip() for p in text.split("\n\n")):
        if not paragraph:
            continue
        while len(paragraph) > max_chars:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(paragraph[:max_chars])
            paragraph = paragraph[max_chars:]
        candidate = f"{current}\n\n{paragraph}" if current else paragraph
        if len(candidate) <= max_chars:
            current = candidate
        else:
            chunks.append(current)
            current = paragraph
    if current:
        chunks.append(current)
    return chunks


class Command(BaseCommand):
    help = "Chunk, embed and store source documents (*.md, *.txt, official_events *.json)."

    def add_arguments(self, parser) -> None:
        parser.add_argument("directory", type=Path)
        parser.add_argument("--batch-size", type=int, default=16)

    def handle(self, *args, directory: Path, batch_size: int, **options) -> None:
        if not directory.is_dir():
            raise CommandError(f"not a directory: {directory}")
        files = sorted(p for p in directory.rglob("*") if p.suffix in SUFFIXES)
        client = FastAPIClient()
        try:
            for path in files:
                self._index_file(path, directory, client, batch_size)
        finally:
            client.close()

    def _index_file(self, path: Path, root: Path, client: FastAPIClient, batch_size: int) -> None:
        raw = path.read_text(encoding="utf-8")
        external_id = str(path.relative_to(root))
        sha = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        if repository.get_document_hash(external_id) == sha:
            self.stdout.write(f"unchanged {external_id}")
            return

        # 0-1.
        if path.suffix == ".json":
            try:
                text = render_events_json(raw)
            except ValueError as exc:  # json.JSONDecodeError is a ValueError
                self.stdout.write(f"skipped   {external_id} ({exc})")
                return
        else:
            text = raw
        chunks = split_into_chunks(text, settings.QA_CHUNK_MAX_CHARS)
        if not chunks:
            self.stdout.write(f"empty     {external_id}")
            return

        # 0-2. outside any transaction; GPU slot per batch.
        vectors: list[list[float]] = []
        model = ""
        for start in range(0, len(chunks), batch_size):
            batch = chunks[start : start + batch_size]
            result = with_infra_retry(on_gpu(lambda batch=batch: client.embed(batch, kind="document")))
            vectors.extend(result.vectors)
            model = result.model

        # 0-3. one transaction.
        repository.save_document(
            external_id=external_id,
            title=path.stem,
            source_uri=external_id,
            content_sha256=sha,
            chunks=list(zip(chunks, vectors, strict=True)),
            embedding_model=model,
        )
        self.stdout.write(f"indexed   {external_id} ({len(chunks)} chunks)")

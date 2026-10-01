"""근거 문서를 색인한다: JSONL -> /embed (ai 게이트웨이) -> source_document / source_chunk.

한 줄에 청크 하나:
  {"url": "https://...", "title": "...", "published_at": "2026-08-27", "chunk_index": 0, "content": "..."}

같은 (url, chunk_index) 의 내용이 그대로고 임베딩이 있으면 건너뛴다(여러 번 실행해도 안전).
문서는 있는 그대로 임베딩한다(질의에만 instruction 을 붙인다).
"""

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from chat.fastapi_client import FastAPIClient
from chat.models import SourceChunk, SourceDocument


class Command(BaseCommand):
    help = "Embed and index source chunks from a JSONL file."

    def add_arguments(self, parser):
        parser.add_argument("path", type=Path)
        parser.add_argument("--batch-size", type=int, default=16)

    def handle(self, *args, path: Path, batch_size: int, **options):
        if not path.exists():
            raise CommandError(f"파일이 없습니다: {path}")
        rows = []
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                r = json.loads(line)
                rows.append((r["url"], r.get("title", ""), r.get("published_at"), int(r["chunk_index"]), r["content"]))
            except (json.JSONDecodeError, KeyError, ValueError) as exc:
                raise CommandError(f"{path}:{number} 형식 오류: {exc!r}") from exc

        pending = []
        unchanged = 0
        for url, title, published_at, index, content in rows:
            existing = SourceChunk.objects.filter(document__url=url, chunk_index=index).first()
            if existing and existing.content == content and existing.embedding is not None:
                unchanged += 1
            else:
                pending.append((url, title, published_at, index, content))

        client = FastAPIClient()
        inserted = updated = 0
        try:
            for start in range(0, len(pending), batch_size):
                batch = pending[start : start + batch_size]
                vectors = client.embed([item[4] for item in batch])
                for (url, title, published_at, index, content), vector in zip(batch, vectors, strict=True):
                    document, _ = SourceDocument.objects.update_or_create(
                        url=url, defaults={"title": title, "published_at": published_at}
                    )
                    _, created = SourceChunk.objects.update_or_create(
                        document=document, chunk_index=index, defaults={"content": content, "embedding": vector}
                    )
                    inserted += created
                    updated += not created
        finally:
            client.close()
        self.stdout.write(f"inserted={inserted} updated={updated} unchanged={unchanged}")

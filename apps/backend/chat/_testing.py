"""테스트 공용 도구. 파일 이름이 `_` 로 시작해서 테스트로 수집되지 않는다."""

from contextlib import contextmanager
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase

from chat.models import Conversation, Message, SourceChunk, SourceDocument

_TABLE_SQL = Path(__file__).resolve().parent.parent / "table.sql"
_schema_ready = False


def ensure_schema() -> None:
    """chat_* 테이블은 managed=False 라서 테스트 DB 에 자동으로 안 생긴다. table.sql 을 적용한다.

    auth_user 는 마이그레이션이 이미 만들었다(table.sql 이 참조). IF NOT EXISTS 라서 반복해도 안전하다.
    """
    global _schema_ready
    if not _schema_ready:
        with connection.cursor() as cursor:
            cursor.execute(_TABLE_SQL.read_text(encoding="utf-8"))
        _schema_ready = True


class ChatTestCase(TestCase):
    @classmethod
    def setUpClass(cls):
        ensure_schema()
        super().setUpClass()

    def make_message(self, question="2026년 8월 기준금리는?", **kwargs) -> Message:
        user = getattr(self, "_user", None) or get_user_model().objects.create_user("u", password="x")
        self._user = user
        conversation = Conversation.objects.create(user=user)
        return Message.objects.create(conversation=conversation, user=user, question=question, **kwargs)

    def make_chunk(self, content, vector, *, url="https://example.test/doc", index=0) -> SourceChunk:
        document, _ = SourceDocument.objects.get_or_create(url=url, defaults={"title": "t"})
        return SourceChunk.objects.create(document=document, chunk_index=index, content=content, embedding=vector)


def unit_vec(index: int) -> list[float]:
    """1.0 이 하나뿐인 벡터. 인덱스가 다르면 서로 직교(코사인 유사도 0)."""
    vector = [0.0] * 1024
    vector[index] = 1.0
    return vector


class FakeClient:
    """FastAPIClient 대역: 호출을 기록하고, 지정하면 실패한다."""

    def __init__(self, *, query_vector=None, answer="", embed_error=None, generate_error=None):
        self.query_vector = query_vector if query_vector is not None else unit_vec(0)
        self.answer = answer
        self.embed_error = embed_error
        self.generate_error = generate_error
        self.embed_texts: list[str] = []
        self.prompts: list[str] = []

    def embed(self, texts):
        self.embed_texts.extend(texts)
        if self.embed_error:
            raise self.embed_error
        return [self.query_vector for _ in texts]

    def generate(self, prompt):
        self.prompts.append(prompt)
        if self.generate_error:
            raise self.generate_error
        return self.answer

    def close(self):
        pass


@contextmanager
def no_gpu_slot():
    yield

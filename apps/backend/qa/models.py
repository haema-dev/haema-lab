"""Django mirror of qa/sql/schema.sql.

The SQL file owns the schema (managed = False), so Django never creates or
alters these tables. Change the SQL first, then follow it here.
"""

from __future__ import annotations

import uuid

from django.db import models
from pgvector.django import VectorField

EMBEDDING_DIMENSIONS = 1024  # qwen3-embedding:0.6b


class SourceDocument(models.Model):
    external_id = models.TextField(unique=True)
    title = models.TextField(default="")
    source_uri = models.TextField(default="")
    content_sha256 = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = "source_document"


class DocumentChunk(models.Model):
    document = models.ForeignKey(SourceDocument, on_delete=models.RESTRICT, related_name="chunks")
    chunk_index = models.IntegerField()
    content = models.TextField()
    embedding = VectorField(dimensions=EMBEDDING_DIMENSIONS)
    embedding_model = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    retired_at = models.DateTimeField(null=True)

    class Meta:
        managed = False
        db_table = "document_chunk"

    @property
    def citation_key(self) -> str:
        """What the answer writes as [c42]; matches core.verify.CITATION_PATTERN."""
        return citation_key(self.id)


def citation_key(chunk_id: int) -> str:
    return f"c{chunk_id}"


class Answer(models.Model):
    answer_text = models.TextField()
    model_name = models.TextField()
    is_fallback = models.BooleanField(default=False)
    verification = models.JSONField()
    question_embedding = VectorField(dimensions=EMBEDDING_DIMENSIONS)
    embedding_model = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        managed = False
        db_table = "answer"


class AnswerCitation(models.Model):
    # Composite PK (answer_id, chunk_id) in SQL; Django 5.2+ maps it with CompositePrimaryKey.
    pk = models.CompositePrimaryKey("answer_id", "chunk_id")
    answer = models.ForeignKey(Answer, on_delete=models.CASCADE, related_name="citations")
    chunk = models.ForeignKey(DocumentChunk, on_delete=models.RESTRICT, related_name="+")
    ordinal = models.IntegerField()

    class Meta:
        managed = False
        db_table = "answer_citation"


class Message(models.Model):
    class Role(models.TextChoices):
        USER = "user"
        ASSISTANT = "assistant"

    user_id = models.BigIntegerField()
    role = models.TextField(choices=Role.choices)
    content = models.TextField(null=True)
    answer = models.ForeignKey(Answer, null=True, on_delete=models.RESTRICT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        managed = False
        db_table = "message"


class Job(models.Model):
    class Status(models.TextChoices):
        QUEUED = "queued"
        RUNNING = "running"
        SUCCEEDED = "succeeded"
        FAILED = "failed"

    class FailureType(models.TextChoices):
        VERIFICATION = "verification"  # counts toward the user's failures
        INFRA = "infra"  # never counts toward the user's failures

    ACTIVE_STATUSES = (Status.QUEUED, Status.RUNNING)

    id = models.UUIDField(primary_key=True, default=uuid.uuid4)
    user_id = models.BigIntegerField()
    question_hash = models.CharField(max_length=64)
    status = models.TextField(choices=Status.choices, default=Status.QUEUED)
    failure_type = models.TextField(choices=FailureType.choices, null=True)
    failure_detail = models.TextField(default="")
    infra_retries = models.IntegerField(default=0)
    user_message = models.OneToOneField(Message, on_delete=models.CASCADE, related_name="+")
    assistant_message = models.OneToOneField(
        Message, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    answer = models.ForeignKey(Answer, null=True, on_delete=models.SET_NULL, related_name="+")
    reused_answer = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True)
    finished_at = models.DateTimeField(null=True)

    class Meta:
        managed = False
        db_table = "job"

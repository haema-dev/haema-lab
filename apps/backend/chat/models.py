"""table.sql의 Django 미러. 스키마는 table.sql이 소유한다(managed = False).

테이블을 바꿀 때는 table.sql을 먼저 고치고 여기를 따라 맞춘다.
"""

from django.conf import settings
from django.contrib.postgres.fields import ArrayField
from django.db import models
from pgvector.django import VectorField

EMBEDDING_DIMENSIONS = 1024  # qwen3-embedding:0.6b


class Conversation(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.DO_NOTHING, db_column="user_id", related_name="+"
    )
    title = models.CharField(max_length=200, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = "chat_conversation"


class Message(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending"
        PROCESSING = "processing"
        DONE = "done"
        FAILED = "failed"
        REJECTED = "rejected"

    class Source(models.TextChoices):
        CACHE = "cache"
        GENERATED = "generated"
        FALLBACK = "fallback"

    conversation = models.ForeignKey(
        Conversation, on_delete=models.DO_NOTHING, db_column="conversation_id", related_name="messages"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.DO_NOTHING, db_column="user_id", related_name="+"
    )
    question = models.CharField(max_length=300, null=True)
    answer = models.TextField(null=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    source = models.CharField(max_length=16, choices=Source.choices, null=True)
    cited_chunk_ids = ArrayField(models.BigIntegerField(), null=True)
    verification = models.JSONField(null=True)
    retry_count = models.SmallIntegerField(default=0)
    error = models.TextField(null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = "chat_message"


class SourceDocument(models.Model):
    url = models.TextField(unique=True)
    title = models.TextField(default="")
    published_at = models.DateField(null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        managed = False
        db_table = "source_document"


class SourceChunk(models.Model):
    document = models.ForeignKey(
        SourceDocument, on_delete=models.DO_NOTHING, db_column="document_id", related_name="chunks"
    )
    chunk_index = models.IntegerField()
    content = models.TextField()
    embedding = VectorField(dimensions=EMBEDDING_DIMENSIONS, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        managed = False
        db_table = "source_chunk"


class AnswerCache(models.Model):
    question = models.CharField(max_length=300)
    question_embedding = VectorField(dimensions=EMBEDDING_DIMENSIONS, null=True)
    answer = models.TextField()
    cited_chunk_ids = ArrayField(models.BigIntegerField(), null=True)
    verification = models.JSONField(null=True)
    hit_count = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = "answer_cache"

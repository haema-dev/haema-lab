from django.contrib import admin

from chat.models import AnswerCache, Conversation, Message, SourceChunk, SourceDocument


@admin.register(Conversation)
class ConversationAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "title", "updated_at")
    raw_id_fields = ("user",)


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = ("id", "conversation", "status", "source", "retry_count", "created_at")
    list_filter = ("status", "source")
    raw_id_fields = ("conversation", "user")


@admin.register(SourceDocument)
class SourceDocumentAdmin(admin.ModelAdmin):
    list_display = ("id", "title", "url", "published_at")
    search_fields = ("title", "url")


@admin.register(SourceChunk)
class SourceChunkAdmin(admin.ModelAdmin):
    list_display = ("id", "document", "chunk_index")
    raw_id_fields = ("document",)
    exclude = ("embedding",)  # 배치로만 채운다. 폼으로 벡터를 편집하지 않는다.


@admin.register(AnswerCache)
class AnswerCacheAdmin(admin.ModelAdmin):
    list_display = ("id", "question", "hit_count", "updated_at")
    search_fields = ("question",)
    exclude = ("question_embedding",)

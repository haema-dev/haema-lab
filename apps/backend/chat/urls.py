from django.urls import path

from chat import views

urlpatterns = [
    path("conversations", views.conversations),
    path("conversations/<int:conversation_id>", views.conversation_detail),
    path("conversations/<int:conversation_id>/messages", views.messages),
    path("messages/<int:message_id>", views.message_detail),
]

"""HTTP views for the backend."""

from __future__ import annotations

from django.http import HttpRequest, HttpResponse


def home(request: HttpRequest) -> HttpResponse:
    return HttpResponse("<h1>Hello World</h1><p>2026년 Django 서버가 정상 작동 중입니다.</p>")

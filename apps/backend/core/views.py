# core/views.py
from django.http import HttpResponse

def home():
    return HttpResponse("<h1>Hello World</h1><p>2026년 Django 서버가 정상 작동 중입니다.</p>")
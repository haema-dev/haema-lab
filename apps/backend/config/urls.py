"""
URL configuration for config project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
# config/urls.py
from django.contrib import admin
from django.urls import include, path
from core import views  # <-- 우리가 만든 core 앱의 views를 가져옵니다.

# Questions go only through api/qa/ (queue -> worker -> FastAPI). The old
# synchronous api/rag/chat called Ollama from the API process, skipping the
# queue, the model gateway and the stored verification, so it is removed.
urlpatterns = [
    path('admin/', admin.site.urls),
    path('home/', views.home),
    path('api/qa/', include('qa.urls')),
]

# 실행 명령어
# uv run uvicorn main:app --reload
#
# 앱 본체는 src/main.py에 있다.

from src.main import app

__all__ = ["app"]

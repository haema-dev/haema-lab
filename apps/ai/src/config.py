from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """환경변수로만 설정한다. 비밀값은 레포에 넣지 않는다 (K8s Secret으로 주입)."""

    # env_ignore_empty: `OLLAMA_BASE_URL=` (값이 빈 줄)을 빈 문자열이 아니라 "없음"으로 본다.
    # 아니면 .env.example을 복사만 해도 필수값 검사를 통과한 채 서버가 뜬다.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_ignore_empty=True)

    host: str = "0.0.0.0"
    port: int = 8000

    # Ollama (Node B, Tailscale 경유). 기본값 없음: 환경변수로만 준다.
    # OLLAMA_BASE_URL, GENERATE_MODEL, EMBED_MODEL 중 하나라도 없으면 기동 시 바로 실패한다.
    ollama_base_url: str
    generate_model: str
    embed_model: str
    # 검색 질의에만 붙이는 instruction. 골든셋 MRR 0.812는 instruction on 상태에서 측정됨.
    # 기본 문구는 측정 때 쓴 문구와 같은지 확인 필요 (추정).
    embed_query_instruction: str = (
        "Given a question, retrieve relevant passages that answer the question"
    )

    # 타임아웃(초). 생성: 긴 RAG 프롬프트 실측 총 111초 + 여유 → 180초 (추정)
    embed_timeout: float = 10.0
    generate_timeout: float = 180.0
    gemini_timeout: float = 60.0
    connect_timeout: float = 5.0

    # Gemini. 판정 모델은 미결정(골든셋 비교 예정)이라 기본값을 두지 않는다.
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta"
    gemini_api_key: SecretStr | None = None
    gemini_judge_model: str | None = None
    gemini_fallback_model: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()

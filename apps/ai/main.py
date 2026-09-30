# 실행 명령어
# uv run uvicorn main:app --reload

from fastapi import FastAPI

app = FastAPI(
    title="Haema Model Gateway",
    description="Stateless model gateway for embedding, generation, and verification.",
    version="0.1.0",
)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/embed")
def embed():
    pass


@app.post("/generate")
def generate():
    pass


@app.post("/judge")
def judge():
    pass

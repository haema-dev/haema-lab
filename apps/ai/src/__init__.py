def main() -> None:
    import uvicorn

    from config import get_settings

    settings = get_settings()
    uvicorn.run("ai.main:app", host=settings.host, port=settings.port)

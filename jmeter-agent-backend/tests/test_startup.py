"""The real application lifespan starts and stops (against conftest's throwaway data directory)."""
import pytest


@pytest.mark.asyncio
async def test_app_starts_registers_lifecycle_listeners_and_shuts_down_cleanly(monkeypatch):
    from app import main
    from app.services import notifications
    from app.services.execution_lifecycle import execution_lifecycle
    import app.services.ai.rag.store as rag_store

    async def no_preload():
        pass  # loading the embedding model takes minutes and is not under test
    monkeypatch.setattr(rag_store, '_preload_embedding_model', no_preload)
    async with main.lifespan(main.app):
        assert notifications._on_finished in execution_lifecycle._listeners['finished']
    assert notifications._on_finished not in execution_lifecycle._listeners['finished']

"""HTTP contracts; ASGI transport never opens a network connection."""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
import httpx
import pytest


@pytest.mark.asyncio
async def test_baseline_accepts_metrics_in_json_and_identity_in_query(monkeypatch):
    from fastapi import FastAPI
    from app.api import baseline_routes
    app = FastAPI()
    app.include_router(baseline_routes.router)
    create = AsyncMock(side_effect=lambda data: data)
    monkeypatch.setattr(baseline_routes.db_store, 'create_baseline', create)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://fixture') as client:
        response = await client.post('/baselines', params={'project_id': 'p', 'name': 'baseline'},
            json={'thresholds': {'p99_max': 42}, 'metrics_snapshot': {'throughput': 5}})
    assert response.status_code == 200
    assert response.json()['thresholds'] == {'p99_max': 42}
    assert response.json()['metrics_snapshot'] == {'throughput': 5}



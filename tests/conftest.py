import os
import sys

import pytest
from fastapi.testclient import TestClient

# Make `src` importable when running pytest from the repo root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import audit  # noqa: E402
from src.auth import mint_token  # noqa: E402
from src.freshness import CACHE  # noqa: E402
from src.main import app  # noqa: E402
from src.ratelimit import LIMITER  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate():
    """Reset shared state so tests don't bleed rate-limit/cache/audit state."""
    LIMITER.reset()
    CACHE.clear()
    audit.clear()
    yield
    LIMITER.reset()
    CACHE.clear()
    audit.clear()


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def eng_headers():
    return {"Authorization": f"Bearer {mint_token('eng', 'acme-corp')}"}


@pytest.fixture
def admin_headers():
    return {"Authorization": f"Bearer {mint_token('admin', 'acme-corp')}"}

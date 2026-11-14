import os
import tempfile
from pathlib import Path

import pytest

# Point the service at an isolated database before importing app code.
_tmp_dir = tempfile.mkdtemp(prefix="tide-test-")
os.environ["TIDEDB_PATH"] = str(Path(_tmp_dir) / "test.db")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def sample_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "sample_data"

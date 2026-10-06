import os
import tempfile

import pytest

# 必须在导入 app 之前设置：每个测试会话使用独立临时库
_tmp = tempfile.mkdtemp(prefix="waterworks-test-")
os.environ["WATERWORKS_DB"] = os.path.join(_tmp, "test.db")
os.environ["SEED_ON_STARTUP"] = "true"

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        yield c

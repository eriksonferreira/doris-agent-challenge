import uuid

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from testkit import make_settings


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(make_settings(tmp_path)))


def conv(client, user="u_ana") -> str:
    cid = str(uuid.uuid4())
    assert client.app.state.store.claim_conversation(cid, user)
    return cid

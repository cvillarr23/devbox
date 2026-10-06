import os
import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))

@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv('DEVBOX_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.setenv('DEVBOX_ENVIRONMENTS', str(tmp_path / 'environments.yaml'))
    monkeypatch.setenv('DEVBOX_WORKSPACE', str(tmp_path))
    return tmp_path

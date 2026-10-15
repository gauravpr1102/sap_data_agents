"""Shared fixtures: a fresh dummy dataset per test session, isolated from data/."""
from __future__ import annotations

import shutil
from types import SimpleNamespace

import pytest

from sapkit import db, synth
from sapkit.config import load_config
from sapkit.mail import MockMailbox

SEED = 7
MATERIALS = 400  # per plant -> 1,200 material-plants; enough to hit every scenario many times


@pytest.fixture(scope="session")
def cfg():
    return load_config()


@pytest.fixture(scope="session")
def dataset(tmp_path_factory, cfg):
    d = tmp_path_factory.mktemp("dataset")
    synth.generate(cfg, d, materials_per_plant=MATERIALS, seed=SEED)
    return d


@pytest.fixture
def workdir(tmp_path, dataset, cfg):
    """A private copy of the database and an empty out/ dir for tests that write."""
    shutil.copy(dataset / f"{cfg.client_id}.duckdb", tmp_path / "db.duckdb")
    shutil.copy(dataset / "mailbox.json", tmp_path / "mailbox.json")
    (tmp_path / "out").mkdir()
    return tmp_path


@pytest.fixture
def con(workdir, cfg):
    c = db.connect(cfg, workdir / "db.duckdb")
    yield c
    c.close()


@pytest.fixture
def mailbox(workdir):
    return MockMailbox(workdir / "mailbox.json", workdir / "out")


# ------------------------------------------------------------------ fake Claude client
def text_block(text):
    return SimpleNamespace(type="text", text=text)


def tool_block(id_, name, input_):
    return SimpleNamespace(type="tool_use", id=id_, name=name, input=input_)


def response(blocks, stop_reason="end_turn"):
    usage = SimpleNamespace(input_tokens=100, output_tokens=20, cache_read_input_tokens=0)
    return SimpleNamespace(content=blocks, stop_reason=stop_reason, usage=usage)


class FakeClient:
    """Replays scripted responses and records every request it receives."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        # Snapshot the message list as sent (it is append-only in the agent).
        self.calls.append({**kwargs, "messages": list(kwargs.get("messages", []))})
        if not self.script:
            raise AssertionError("FakeClient ran out of scripted responses")
        nxt = self.script.pop(0)
        return nxt(kwargs) if callable(nxt) else nxt


@pytest.fixture
def fake():
    return SimpleNamespace(FakeClient=FakeClient, text=text_block, tool=tool_block, response=response)

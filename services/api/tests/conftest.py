"""Session-wide fixtures: one event loop, DB, plugins, scripted models and the run manager."""

import asyncio

import pytest

from todd import llm, registry
from todd.db import init_db
from todd.orchestrator import manager

from .helpers import SCRIPTS, ScriptedModel


@pytest.fixture(scope="session")
def loop():
    lp = asyncio.new_event_loop()
    yield lp
    lp.close()


@pytest.fixture(scope="session", autouse=True)
def setup(loop):
    init_db()
    from todd import settings

    settings.update({"engine": "api"})  # the LangGraph engine with scripted models; test_claude_code switches engines
    registry.load_plugins()
    llm.set_chat_model_factory(lambda role, agent_name=None: ScriptedModel(role=role, agent_name=agent_name, scripts=SCRIPTS))
    loop.run_until_complete(manager.startup())
    yield
    loop.run_until_complete(manager.shutdown())



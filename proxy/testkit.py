"""Test helpers shared by proxy/tests and eval/tests. Not used by the running app."""
from dataclasses import replace
from pathlib import Path

from app.settings import Settings


def make_settings(tmp_path: Path, **over) -> Settings:
    return replace(Settings(), db_path=str(tmp_path / "proxy.db"), latency_scale=0.0, **over)


import inspect
import json

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse


def make_fake_agent(behavior) -> FastAPI:
    """A stand-in agent. `behavior(body, proxy)` returns a Response, or an async iterator of
    event dicts (sent as SSE `data:` lines) and raw strings (sent verbatim)."""
    agent = FastAPI()
    agent.state.proxy = None
    agent.state.received = []

    @agent.post("/chat")
    async def chat(request: Request):
        body = await request.json()
        agent.state.received.append(body)
        result = behavior(body, agent.state.proxy)
        if inspect.isawaitable(result):
            result = await result
        if isinstance(result, Response):
            return result

        async def gen():
            async for ev in result:
                yield ev if isinstance(ev, str) else f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    return agent


def wire(tmp_path: Path, behavior, **settings_over) -> FastAPI:
    """Proxy app whose agent is a fake agent that calls back into this same proxy."""
    from app.main import create_app

    agent = make_fake_agent(behavior)
    proxy = create_app(make_settings(tmp_path, **settings_over), agent_transport=httpx.ASGITransport(agent))
    agent.state.proxy = httpx.AsyncClient(transport=httpx.ASGITransport(proxy), base_url="http://proxy")
    proxy.state.fake_agent = agent
    return proxy

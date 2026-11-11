"""Chat gateway: forwards the turn to the agent and streams back one SSE stream that merges the
agent's text/feedback events with artifact events from /tools/show_artifact. Cuts the turn at
TURN_TIMEOUT_S and always ends with exactly one `done` or `error`."""
import asyncio
import json

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from app.seed import persona_of
from app.store import TurnInProgress
from app.tools import error_body

router = APIRouter()
FEEDBACK_STATES = {"running", "done", "error"}


class ChatIn(BaseModel):
    user_id: str
    conversation_id: str
    message: str
    images: list[str] = []


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


def _error(code: str, message: str) -> dict:
    return {"type": "error", "code": code, "message": message}


def _clean(ev) -> dict | None:
    """Agent event → client event, or None to skip it."""
    if not isinstance(ev, dict):
        return None
    kind = ev.get("type")
    if kind == "text" and isinstance(ev.get("delta"), str):
        return {"type": "text", "delta": ev["delta"]}
    if kind == "feedback" and isinstance(ev.get("id"), str) and isinstance(ev.get("text"), str):
        state = ev.get("state") if ev.get("state") in FEEDBACK_STATES else "running"
        return {"type": "feedback", "id": ev["id"], "text": ev["text"], "state": state}
    if kind == "done":
        return {"type": "done"}
    if kind == "error":
        return _error("agent_error", str(ev.get("message") or "erro no agente"))
    return None


async def _pump_agent(agent: httpx.AsyncClient, body: dict, out: asyncio.Queue) -> None:
    try:
        async with agent.stream("POST", "/chat", json=body) as r:
            if r.status_code != 200:
                out.put_nowait(_error("agent_error", f"o agente respondeu HTTP {r.status_code}"))
                return
            async for line in r.aiter_lines():
                if not line.startswith("data:"):
                    continue
                try:
                    ev = _clean(json.loads(line[5:].strip()))
                except ValueError:
                    continue
                if ev is None:
                    continue
                out.put_nowait(ev)
                if ev["type"] in ("done", "error"):
                    return
        out.put_nowait(_error("agent_error", "o agente encerrou a resposta sem enviar done"))
    except httpx.ConnectError as e:
        out.put_nowait(_error("agent_unavailable", f"não foi possível conectar ao agente: {e}"))
    except httpx.HTTPError as e:
        out.put_nowait(_error("agent_error", f"falha na conexão com o agente: {e}"))
    except Exception as e:  # noqa: BLE001 — never leave the client waiting for the turn cut
        out.put_nowait(_error("agent_error", f"erro inesperado ao falar com o agente: {e!r}"))


async def _stream(st, body: ChatIn, turn_id: int, out: asyncio.Queue):
    cid = body.conversation_id
    pump = asyncio.create_task(_pump_agent(st.agent, body.model_dump(), out))
    text: list[str] = []
    n_artifacts, outcome = 0, "error"
    try:
        async with asyncio.timeout(st.settings.turn_timeout_s):
            while True:
                ev = await out.get()
                if ev["type"] == "text":
                    text.append(ev["delta"])
                elif ev["type"] == "artifact":
                    n_artifacts += 1
                elif ev["type"] == "done":
                    while not out.empty():  # artifacts published just before done
                        extra = out.get_nowait()
                        n_artifacts += extra["type"] == "artifact"
                        yield _sse(extra)
                    outcome = "done"
                yield _sse(ev)
                if ev["type"] in ("done", "error"):
                    break
    except TimeoutError:
        outcome = "turn_timeout"
        yield _sse(_error("turn_timeout", f"o turno passou de {st.settings.turn_timeout_s:g}s e foi encerrado"))
    finally:
        pump.cancel()
        st.bus.unsubscribe(cid)
        st.store.end_turn(turn_id, outcome, "".join(text), n_artifacts)


@router.post("/chat")
async def chat(body: ChatIn, request: Request):
    st = request.app.state
    if persona_of(body.user_id) not in st.seed.users:
        return JSONResponse(error_body("unknown_user", f"usuário {body.user_id} não existe"), status_code=400)
    if not st.store.claim_conversation(body.conversation_id, body.user_id):
        return JSONResponse(error_body("conversation_owned", "esta conversa pertence a outro usuário"), status_code=409)
    try:
        turn_id = st.store.begin_turn(body.conversation_id, body.user_id, body.message)
    except TurnInProgress:
        return JSONResponse(error_body("turn_in_progress", "já existe um turno em andamento nesta conversa", True),
                            status_code=409)
    out = st.bus.subscribe(body.conversation_id)
    return StreamingResponse(_stream(st, body, turn_id, out), media_type="text/event-stream")

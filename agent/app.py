"""Agente inicial (stub). Substitua por completo — veja o README na raiz para o contrato."""
import json

from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

app = FastAPI()


class ChatIn(BaseModel):
    user_id: str
    conversation_id: str
    message: str
    images: list[str] = []


@app.get("/health")
async def health():
    return {"ok": True}


@app.post("/chat")
async def chat(body: ChatIn):
    async def events():
        for ev in ({"type": "text", "delta": "Ainda não fui implementado 🙂"}, {"type": "done"}):
            yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")

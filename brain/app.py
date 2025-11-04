#!/usr/bin/env python3
# FastAPI "brain" gateway: proxies OpenAI-compatible routes to local LLM runtime
import os
import asyncio
from typing import Optional, Set

import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse

LLM_API_BASE = os.getenv("LLM_API_BASE", "http://host.docker.internal:1234/v1").rstrip("/")
LLM_API_KEY  = os.getenv("LLM_API_KEY", "local")
DEFAULT_MODEL = os.getenv("MODEL", "llama-3.1-8b-instruct")
_allowed = os.getenv("ALLOWED_MODELS", "")
ALLOWED_MODELS: Optional[Set[str]] = set(m.strip() for m in _allowed.split(",")) if _allowed else None

app = FastAPI(title="brain-gateway", version="1.0.0")

def _auth_headers():
    # Many local runtimes ignore the key; harmless to send it.
    return {"Authorization": f"Bearer {LLM_API_KEY}"}

async def _forward_json(method: str, path: str, json=None, params=None):
    url = f"{LLM_API_BASE}{path}"
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.request(method, url, headers=_auth_headers(), json=json, params=params)
        if r.status_code >= 400:
            # bubble up LLM error payload
            raise HTTPException(status_code=r.status_code, detail=r.text)
        return JSONResponse(status_code=r.status_code, content=r.json())

@app.get("/healthz")
async def healthz():
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{LLM_API_BASE}/models", headers=_auth_headers())
            ok = r.status_code == 200
            models = r.json().get("data", []) if ok else []
        return {"ok": ok, "llm_base": LLM_API_BASE, "models": [m.get("id") for m in models]}
    except Exception as e:
        return {"ok": False, "llm_base": LLM_API_BASE, "error": repr(e)}

@app.get("/v1/models")
async def list_models():
    return await _forward_json("GET", "/models")

@app.post("/v1/chat/completions")
async def chat_completions(req: Request):
    body = await req.json()
    # Per-request override, with allowlist if provided
    model = (body.get("model") or DEFAULT_MODEL).strip()
    if ALLOWED_MODELS and model not in ALLOWED_MODELS:
        raise HTTPException(status_code=400, detail=f"model '{model}' not in ALLOWED_MODELS")
    body["model"] = model
    return await _forward_json("POST", "/chat/completions", json=body)

@app.post("/v1/embeddings")
async def embeddings(req: Request):
    body = await req.json()
    model = (body.get("model") or "nomic-embed-text-v1.5").strip()
    if ALLOWED_MODELS and model not in ALLOWED_MODELS:
        raise HTTPException(status_code=400, detail=f"model '{model}' not in ALLOWED_MODELS")
    body["model"] = model
    return await _forward_json("POST", "/embeddings", json=body)

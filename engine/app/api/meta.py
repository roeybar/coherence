"""Health and public configuration."""

from fastapi import APIRouter, Request

from app import ratelimit
from app.providers import registry

router = APIRouter()


@router.get("/api/health")
def health():
    return {"ok": True}


@router.get("/api/config")
def config(request: Request):
    return {
        "mode": request.app.state.mode,
        "providers": list(registry.REAL_PROVIDERS),
        "tuned": "anthropic",
        "message_interval_seconds": ratelimit.messages.interval,
    }

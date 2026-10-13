"""Self-host key store routes. Never mounted in hosted mode."""

import secrets

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field

from app import ratelimit

router = APIRouter(prefix="/api/keys")
COOKIE = "coh_session"


class UnlockIn(BaseModel):
    passphrase: str = Field(min_length=1, max_length=1024)


class KeyIn(BaseModel):
    key: str = Field(min_length=1, max_length=4096)


def _session(request: Request) -> str:
    return request.cookies.get(COOKIE) or ""


@router.post("/unlock")
def unlock(body: UnlockIn, request: Request, response: Response):
    from app import creds

    ratelimit.unlocks.check(ratelimit.client_ip(request))
    session = secrets.token_urlsafe(32)
    try:
        creds.vault().unlock(session, body.passphrase)
    except creds.WrongPassphraseOrCorrupt:
        raise ratelimit.api_error(403, "wrong_passphrase") from None
    except OSError:
        raise ratelimit.api_error(500, "key_store_unavailable") from None
    old = _session(request)
    if old:
        creds.vault().lock(old)
    response.set_cookie(COOKIE, session, httponly=True, samesite="strict", path="/")
    return {"ok": True}


@router.post("/lock")
def lock(request: Request, response: Response):
    from app import creds

    if _session(request):
        creds.vault().lock(_session(request))
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


def _call(request: Request, fn):
    from app import creds

    try:
        return fn(creds.vault(), _session(request))
    except creds.Locked:
        raise ratelimit.api_error(428, "key_store_locked") from None
    except creds.WrongPassphraseOrCorrupt:
        raise ratelimit.api_error(403, "wrong_passphrase") from None
    except OSError:
        raise ratelimit.api_error(500, "key_store_unavailable") from None


def _known(provider: str) -> None:
    from app import creds

    if provider not in creds.PROVIDERS:
        raise ratelimit.api_error(404, "unknown_provider")


@router.get("/status")
def status(request: Request):
    from app import creds

    return _call(request, lambda v, s: v.status(s, creds.PROVIDERS))


@router.put("/{provider}")
def put_key(provider: str, body: KeyIn, request: Request):
    _known(provider)
    _call(request, lambda v, s: v.put(s, provider, body.key))
    return {"ok": True}


@router.delete("/{provider}")
def delete_key(provider: str, request: Request):
    _known(provider)
    _call(request, lambda v, s: v.delete(s, provider))
    return {"ok": True}

"""The engine's only seam to simple-creds-storage (self-host mode only)."""

import threading
from pathlib import Path
from typing import Optional

from pydantic import SecretStr
from simple_creds_storage import CredStore, Locked, Vault, WrongPassphraseOrCorrupt

from app.config import settings

PROVIDERS = ("anthropic", "openai", "gemini", "xai")
STORE_FILE = "keys.v1.json"
__all__ = ["PROVIDERS", "Locked", "WrongPassphraseOrCorrupt", "key_state", "vault"]

_vault: Optional[Vault] = None
_lock = threading.Lock()


def vault() -> Vault:
    global _vault
    with _lock:
        if _vault is None:
            store = CredStore(Path(settings.keys_dir) / STORE_FILE)
            _vault = Vault(
                store, settings.vault_idle_ttl_s, max_sessions=settings.vault_max_sessions
            )
        return _vault


def key_state(
    session_cookie: Optional[str], provider: str
) -> tuple[str, Optional[SecretStr], tuple]:
    if provider not in PROVIDERS:
        return "none", None, ()
    try:
        v = vault()
        if not session_cookie:
            raise Locked()
        secret = v.get(session_cookie, provider)
        if secret is not None:
            return "ok", SecretStr(secret.reveal()), ()
        flags = v.status(session_cookie, PROVIDERS)
    except Locked:
        return "locked" if v.store.exists() else "none", None, ()
    others = tuple(p for p in PROVIDERS if flags.get(p))
    if others:
        return "other", None, others
    return "none", None, ()

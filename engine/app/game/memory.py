"""Long-term memory: embed turn chunks, retrieve relevant ones at recall time.

Uses fastembed (ONNX, no torch) -> bge-small-en-v1.5 (384-dim, ~33MB).
Stores entries in the bound save doc under "memory" as
{turn, role, text, v}, where v is base64 of float16[384]. Recall is a
numpy cosine over those entries. The save_id arguments do not select the
doc; the bound save doc is the scope.

Failure of any retrieval/index call must not break a turn - log and continue.
"""

import base64
import logging
from typing import Optional

import numpy as np
from fastembed import TextEmbedding

from app.store import current_save


log = logging.getLogger(__name__)

EMBED_MODEL = "BAAI/bge-small-en-v1.5"
VECTOR_SIZE = 384
MAX_CHUNK = 2000  # truncate stored content
MAX_QUERY = 1000  # truncate query text before embed


_embedder: Optional[TextEmbedding] = None


def _disable_ort_telemetry() -> None:
    import onnxruntime

    try:
        onnxruntime.disable_telemetry_events()
    except AttributeError:
        pass


def _embed_model() -> TextEmbedding:
    global _embedder
    if _embedder is None:
        _disable_ort_telemetry()
        _embedder = TextEmbedding(EMBED_MODEL)
    return _embedder


def _embed(text: str) -> list[float]:
    return list(next(_embed_model().embed([text[:MAX_QUERY]])))


def _encode(vec) -> str:
    return base64.b64encode(np.asarray(vec, dtype=np.float16).tobytes()).decode("ascii")


def _decode(v: str) -> np.ndarray:
    return np.frombuffer(base64.b64decode(v), dtype=np.float16).astype(np.float32)


def index_turn(save_id: str, turn: int, role: str, content: str) -> None:
    text = (content or "").strip()
    if not text:
        return
    try:
        entries = current_save()["memory"]
        entry = {
            "turn": turn,
            "role": role,
            "text": text[:MAX_CHUNK],
            "v": _encode(_embed(text)),
        }
        for i, e in enumerate(entries):
            if e.get("turn") == turn and e.get("role") == role:
                entries[i] = entry
                break
        else:
            entries.append(entry)
    except Exception as e:
        log.warning("memory.index_turn failed: %s", e)


def recall(
    save_id: str, query: str, top_k: int = 4, exclude_after_turn: Optional[int] = None
) -> list[dict]:
    """Returns up to top_k relevant past chunks for the bound save, sorted by turn ascending."""
    text = (query or "").strip()
    if not text:
        return []
    try:
        entries = current_save()["memory"]
        if exclude_after_turn is not None:
            entries = [e for e in entries if e.get("turn", 0) < exclude_after_turn]
        if not entries:
            return []
        q = np.asarray(_embed(text), dtype=np.float32)
        mat = np.stack([_decode(e["v"]) for e in entries])
        denom = np.linalg.norm(mat, axis=1) * (np.linalg.norm(q) or 1.0)
        scores = (mat @ q) / np.where(denom == 0, 1.0, denom)
        order = np.argsort(-scores, kind="stable")[: top_k * 4]
        results = [(entries[int(i)], float(scores[int(i)])) for i in order]
    except Exception as e:
        log.warning("memory.recall failed: %s", e)
        return []

    chunks: list[dict] = []
    seen_turns = set()
    for p, score in results:
        turn = p.get("turn", 0)
        if exclude_after_turn is not None and turn >= exclude_after_turn:
            continue
        if turn in seen_turns:
            continue
        seen_turns.add(turn)
        chunks.append(
            {
                "turn": turn,
                "role": p.get("role", ""),
                "content": p.get("text", ""),
                "score": score,
            }
        )
        if len(chunks) >= top_k:
            break

    return sorted(chunks, key=lambda c: c["turn"])


def format_recall_block(chunks: list[dict]) -> str:
    """Render recalled chunks for inclusion in the user message context."""
    if not chunks:
        return ""
    lines = ["[RELEVANT MEMORY - earlier in this run, surfaced by topical similarity]"]
    for c in chunks:
        snippet = c["content"][:400].replace("\n", " ").strip()
        role = "you" if c["role"] == "assistant" else "player"
        lines.append(f"- turn {c['turn']} ({role}): {snippet}")
    lines.append("[/RELEVANT MEMORY]")
    return "\n".join(lines)

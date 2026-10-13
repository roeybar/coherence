"""Per-save context engine - token tracking, history compression.

The context is reconstructed from the save's session_in/session_out fields on
each request, mutated, then written back.
"""

import json
import logging
from typing import Callable, Optional

from app.config import settings
from app.game import memory as memory_mod
from app.game.llm import MODEL_HAIKU, cache_split_system
from app.llm_client import call_messages
from app.game.prompts import COMPRESS_PROMPT, build_system_blocks
from app.providers import registry


log = logging.getLogger(__name__)


COMPRESS_AT = 20  # compress when stored history exceeds this many messages
COMPRESS_KEEP = 8  # keep this many recent messages raw after compression
TOKEN_BUDGET = 8000  # input-token estimate that maps to health 0


class ContextEngine:
    def __init__(self, session_in: int = 0, session_out: int = 0, tier: str = "basic"):
        self.session_in = session_in
        self.session_out = session_out
        self.tier = tier
        # Cache token accumulators - display-only, not persisted to GameSave.
        # Summed across every LLM call in the session for the status overlay.
        self.session_cache_read = 0
        self.session_cache_create = 0

    def record(self, usage):
        if usage:
            self.session_in += usage.input_tokens
            self.session_out += usage.output_tokens
            self.session_cache_read += getattr(usage, "cache_read_input_tokens", 0)
            self.session_cache_create += getattr(usage, "cache_creation_input_tokens", 0)

    def health_score(self, system: str, messages: list) -> int:
        chars = len(system) + sum(
            len(m.get("content") or m.get("content_en") or "") for m in messages
        )
        estimated = chars // 4
        return max(0, 100 - int(estimated / TOKEN_BUDGET * 100))

    def monster_level(self) -> int:
        # Always 0. The anti-automation detector (game/anti_automation.py)
        # drives the effective monster level.
        return 0

    def maybe_compress(self, history: list) -> list:
        if len(history) <= COMPRESS_AT:
            return history
        to_compress = history[: len(history) - COMPRESS_KEEP]
        keep = history[len(history) - COMPRESS_KEEP :]

        def _en(m: dict) -> str:
            return m.get("content_en") or m.get("content") or ""

        text = "\n".join(f"[{m['role'].upper()}]: {_en(m)[:500]}" for m in to_compress)
        try:
            resp = call_messages(
                model=MODEL_HAIKU,
                max_tokens=300,
                system=None,
                messages=[{"role": "user", "content": COMPRESS_PROMPT.format(text=text)}],
            )
            self.record(resp.usage)
            summary = "".join(b.text for b in resp.content if hasattr(b, "text")).strip()
            # Summary stays in English (canonical). Display layer falls back
            # to content_en if no locale-specific version exists.
            return [
                {
                    "role": "assistant",
                    "content_en": f"[HISTORY SUMMARY - {len(to_compress)} messages]\n{summary}",
                }
            ] + keep
        except Exception:
            return history[-COMPRESS_KEEP:]


MESSAGE_OVERHEAD = 4
ANTHROPIC_HIST_HIGH = 70_000


class ContextTooLarge(Exception):
    """Story block plus this move's context exceed the ceiling with no history sent."""

    def __init__(self, est_tokens: int, high: int):
        super().__init__(f"context estimate {est_tokens} over ceiling {high}")
        self.est_tokens, self.high = est_tokens, high


def est(chars: int) -> int:
    return -(-chars // 3)


def ceiling_for(hist_high: int) -> tuple[int, int, int]:
    return (hist_high + 20_000, max(1, 3 * hist_high // 7), hist_high)


def context_ceiling() -> Optional[tuple[int, int, int]]:
    provider = registry.current_provider()
    if provider == "anthropic":
        return ceiling_for(ANTHROPIC_HIST_HIGH)
    if provider == "fake" and registry.fake_enabled():
        n = settings.engine_fake_context_tokens
        if n is not None and n > 0:
            return ceiling_for(n)
    return None


def _size(m: dict) -> int:
    return len(m.get("content") or "") + MESSAGE_OVERHEAD


def candidates(history: list, step: int) -> list[int]:
    out, chars, mark, pending = [0], 0, step, False
    for i, m in enumerate(history):
        if pending and i > 0 and m.get("role") == "user":
            out.append(i)
            pending = False
        chars += _size(m)
        while est(chars) >= mark:
            pending = True
            mark += step
    return out


def window_start(history: list, ceiling: tuple) -> int:
    _high, step, hist_high = ceiling
    sizes = [_size(m) for m in history]
    suffix = sum(sizes)
    prev = 0
    for c in candidates(history, step):
        suffix -= sum(sizes[prev:c])
        prev = c
        if est(suffix) <= hist_high:
            return c
    return len(history)


def turn_at(history: list, turn: int, i: int) -> int:
    if i == 0 and history and history[0].get("role") == "assistant":
        return 0
    return turn - (len(history) - 1 - i) // 2


def recall_cutoff(history: list, turn: int, start: int) -> Optional[int]:
    if start <= 0:
        return None
    if start >= len(history):
        return turn + 1
    return turn_at(history, turn, start)


def recall_block(history, turn, save_id, query, start) -> tuple[list, str]:
    cutoff = recall_cutoff(history, turn, start)
    if cutoff is None:
        return [], ""
    hits = memory_mod.recall(save_id, query, top_k=4, exclude_after_turn=cutoff)
    return hits, memory_mod.format_recall_block(hits)


def recall_for(ts) -> Callable[[int], str]:
    return lambda start: recall_block(
        ts.gs.history,
        ts.gs.turn,
        ts.save_id,
        ts.recall_query,
        start,
    )[1]


def assemble(
    stable, volatile, history, build_context, recall_for_start, *, start, memory_block, legacy_n
):
    """history is the _history_for_llm projection. Returns (system, messages, start, memory_block).
    The guard only moves start forward and is recomputed per call (not sticky)."""
    ceiling = context_ceiling()
    if ceiling is None:
        messages = history[-legacy_n:]
        messages.append({"role": "user", "content": build_context(memory_block)})
        return cache_split_system(stable, volatile), messages, start, memory_block
    high, step, _ = ceiling
    stops = [c for c in candidates(history, step) if c > start] + [len(history)]
    while True:
        ctx_msg = build_context(memory_block)
        final = f"{volatile}\n\n{ctx_msg}" if volatile else ctx_msg
        messages = history[start:] + [{"role": "user", "content": final}]
        total = est(len(stable) + sum(_size(m) for m in messages))
        if total <= high:
            return cache_split_system(stable, ""), messages, start, memory_block
        if start >= len(history):
            raise ContextTooLarge(total, high)
        nxt = next(c for c in stops if c > start)
        log.warning("context window: estimate %d over %d, start %d -> %d", total, high, start, nxt)
        start, memory_block = nxt, recall_for_start(nxt)


def check_floor(gs, player_input: str) -> None:
    """Refuse before any model call when story block + world state + move exceed HIGH."""
    ceiling = context_ceiling()
    if ceiling is None:
        return
    stage = gs.state.get("tutorial_stage", 0)
    chars = len(player_input or "") + MESSAGE_OVERHEAD
    if gs.profile is not None:
        chars += len(
            build_system_blocks(
                gs.profile,
                stage,
                gs.config,
                seed_roster=gs.seed_roster,
            )[0]
        )
    chars += len(f"[WORLD STATE]\n{json.dumps(gs.state.to_dict(), indent=2)}\n[/WORLD STATE]") + 2
    if est(chars) > ceiling[0]:
        raise ContextTooLarge(est(chars), ceiling[0])

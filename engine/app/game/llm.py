"""LLM call wrappers.

Game-side code never imports a vendor SDK. Every call goes through
app.llm_client.call_messages, which sends it to the provider bound to
the current request. Synthetic Message-shaped responses keep the rest
of the engine provider-neutral.
"""

from app.llm_client import call_messages
from app.providers import registry
from app.game.prompts import (
    PREFILL_NARRATIVE,
    ability_keywords,
    rewind_system,
    router_system,
    world_gen_prompt,
)


# Model tier aliases. The provider adapter resolves them to real model
# ids from {PROVIDER}_MODEL_FAST / {PROVIDER}_MODEL_STRONG in env.
MODEL_HAIKU = "tier:fast"
MODEL_SONNET = "tier:strong"
MAX_TOKENS = 1024


# Anthropic prompt cache wrap: a system prefix that is byte-identical
# across calls within ~5 min hits the ephemeral cache, billing at ~10%
# of normal input rate (the stable pack prefix is ~4.7k tokens, well
# above the provider's minimum cacheable length). The
# Anthropic adapter forwards the cache_control marker unchanged.
def _cached_system(system) -> list:
    """Wrap a string system into a single cache-eligible content block."""
    if isinstance(system, str):
        if not system:
            return []
        return [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
    return system


def cache_split_system(stable: str, volatile: str) -> list:
    """Two-block system for the DM beats: the cache breakpoint sits at the
    END of the stable pack prefix, so the per-turn volatile half (npc /
    abilities / monster / addressee / world-systems / pack beats / dm_notes)
    can change freely without busting the cached prefix. A single-block
    system with the breakpoint after everything - the previous shape - caches nothing whenever any volatile block differs from the last turn."""
    blocks: list = []
    if stable:
        blocks.append({"type": "text", "text": stable, "cache_control": {"type": "ephemeral"}})
    if volatile:
        blocks.append({"type": "text", "text": volatile})
    return blocks


# Message/history cache breakpoint
# The second Anthropic cache lever: mark the last history message
# (messages[-2]) with cache_control so the conversation history is
# cached across intra-turn beats. The current
# turn's user message (messages[-1]) is not marked - it carries
# per-beat-varying content (profile directive, dice) that differs
# between beats and must sit after the breakpoint.
#
# prefix contract: per-beat-varying content is never before the breakpoint.
# Confirmed by TestCachePrefixInvariant in test_narration_discipline.py.
#
# Only applied when the bound provider supports cache_control.
# No-op when messages has <2 entries (no history to mark).
# Returns a new list; the input is never mutated.


def _mark_history_cache_breakpoint(messages: list, model: str) -> list:
    """Add cache_control to the last history message.

    Marks messages[-2] - the final message from the history prefix - so the history is cached by Anthropic's ephemeral prefix cache.
    messages[-1] (the new per-turn user message) is left untouched:
    it may contain per-beat directives and insight-dice that vary.

    Handles both string content (converted to single text block) and
    existing list content (cache_control appended to last block).
    Provider without cache_control support -> no-op.
    """
    if not registry.supports_cache_control(model):
        return messages
    if len(messages) < 2:
        return messages
    result = list(messages)
    prev = result[-2]
    content = prev.get("content")
    if isinstance(content, str) and content:
        # Convert string content to a single cached block.
        marked = dict(prev)
        marked["content"] = [
            {"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}
        ]
        result[-2] = marked
    elif isinstance(content, list) and content:
        # Add cache_control to the last block in the existing list.
        new_blocks = list(content)
        last_block = dict(new_blocks[-1])
        last_block["cache_control"] = {"type": "ephemeral"}
        new_blocks[-1] = last_block
        marked = dict(prev)
        marked["content"] = new_blocks
        result[-2] = marked
    return result


def call_llm_reply(
    system: str, messages: list, model: str | None = None, max_tokens: int = MAX_TOKENS
) -> tuple[str, object, str, bool]:
    """Non-streaming call through the provider bound to the current request.
    Returns (raw_text, usage, stop_reason, truncated)."""
    use_model = model or MODEL_HAIKU
    use_prefill = registry.supports_prefill(use_model)

    # Add history cache breakpoint on messages[-2]
    # before appending the prefill (so the prefill sits after the mark).
    messages_with_bp = _mark_history_cache_breakpoint(messages, use_model)
    api_messages = (
        messages_with_bp + [{"role": "assistant", "content": PREFILL_NARRATIVE}]
        if use_prefill
        else messages_with_bp
    )
    resp = call_messages(
        model=use_model,
        max_tokens=max_tokens,
        system=_cached_system(system),
        messages=api_messages,
    )
    text = "".join(block.text for block in resp.content if hasattr(block, "text"))
    raw = (PREFILL_NARRATIVE if use_prefill else "") + text
    return (
        raw,
        resp.usage,
        getattr(resp, "stop_reason", "") or "",
        bool(getattr(resp, "truncated", False)),
    )


def call_llm_plain(
    system: str, messages: list, model: str | None = None, max_tokens: int = 350
) -> tuple[str, object]:
    """No prefill, no wrapping. For meta and rewind responses."""
    use_model = model or MODEL_HAIKU
    resp = call_messages(
        model=use_model,
        max_tokens=max_tokens,
        system=_cached_system(system),
        messages=messages,
    )
    text = "".join(block.text for block in resp.content if hasattr(block, "text"))
    return text, resp.usage


def classify_turn(player_input: str, config=None) -> tuple[str, object | None]:
    """Returns (label, usage). Label is 'ability' or 'standard'."""
    keywords = ability_keywords(config)
    words = set(player_input.lower().split())
    if not (words & keywords):
        return "standard", None
    try:
        resp = call_messages(
            model=MODEL_HAIKU,
            max_tokens=5,
            system=router_system(config),
            messages=[{"role": "user", "content": player_input}],
        )
        label = "".join(b.text for b in resp.content if hasattr(b, "text")).strip().lower()
        return ("ability" if label == "ability" else "standard"), resp.usage
    except Exception:
        return "standard", None


# stakes classifier
# Separate from classify_turn so the ability router's prompt stays
# byte-identical (cache-stable). Keyword fast-path covers clearly
# mundane actions without an LLM call; ambiguous inputs fall through
# to a tiny 5-token Haiku call. Fail-safe: any exception -> "normal"
# (today's behaviour; must never raise or cause a 500).

# Verbs whose presence as the first content word unambiguously signals
# a mundane / low-stakes action. Stripped of leading "I " pronoun.
_MUNDANE_VERBS: frozenset[str] = frozenset(
    {
        "smoke",
        "light",
        "sit",
        "stand",
        "lie",
        "rest",
        "wait",
        "sleep",
        "eat",
        "drink",
        "read",
        "write",
        "go",
        "head",
        "walk",
        "return",
        "leave",
        "enter",
        "come",
        "check",
        "look",
        "inspect",
        "glance",
        "open",
        "close",
        "nod",
        "shrug",
        "sigh",
        "stretch",
        "pick",
        "put",
        "set",
        "place",
        "take",
        "hand",
        "give",
        "light",
        "pour",
    }
)

# Two-word phrases that are unambiguously mundane movement.
_MUNDANE_BIGRAMS: frozenset[str] = frozenset(
    {
        "go to",
        "go back",
        "head to",
        "head back",
        "walk to",
        "walk back",
        "come back",
    }
)

_STAKES_ROUTER_PROMPT = (
    "Classify the player action's STAKES for narration length. "
    "Respond with exactly one word, no punctuation:\n"
    "  mundane  - routine, low-stakes: travel to a known place, smoking, "
    "sitting, resting, checking something familiar, simple routine "
    "physical actions that do not change the situation.\n"
    "  dramatic - high-stakes: reveals, confrontations, first encounters "
    "with named NPCs, life-or-death moments, plot turns, major decisions.\n"
    "  normal   - everything else.\n"
    "DEFAULT TO NORMAL when unsure."
)


def classify_stakes(player_input: str, config=None) -> tuple[str, object | None]:
    """Returns (label, usage). Label is 'mundane', 'normal', or 'dramatic'.

    Fast-path: first-word keyword match for unambiguously mundane actions - no LLM call. Falls through to a tiny 5-token Haiku call for ambiguous
    inputs. Fail-safe: any exception -> 'normal' (today's behaviour; this
    function sits in run_turn outer scope and must not raise).
    """
    try:
        words = (player_input or "").lower().split()
        # Strip leading "i " pronoun before verb matching.
        if words and words[0] == "i":
            words = words[1:]
        if not words:
            return "normal", None
        # Fast-path - mundane first verb (no LLM call).
        if words[0] in _MUNDANE_VERBS:
            return "mundane", None
        # Fast-path - mundane two-word phrase.
        bigram = f"{words[0]} {words[1]}" if len(words) >= 2 else ""
        if bigram in _MUNDANE_BIGRAMS:
            return "mundane", None
        # Ambiguous -> tiny Haiku call.
        resp = call_messages(
            model=MODEL_HAIKU,
            max_tokens=5,
            system=_STAKES_ROUTER_PROMPT,
            messages=[{"role": "user", "content": player_input}],
        )
        label = "".join(b.text for b in resp.content if hasattr(b, "text")).strip().lower()
        if label in ("mundane", "dramatic"):
            return label, resp.usage
        return "normal", resp.usage
    except Exception:
        # Fail-safe: degrade to "normal" (today's behaviour).
        return "normal", None


def generate_world_premise(hint: str, config=None) -> str:
    template = world_gen_prompt(config)
    prompt = template.format(hint=hint if hint else "Surprise me - be creative and unusual.")
    # bump output budget from 512 -> 5000 so haiku can write a
    # richer JSON premise when the player fills the structured form
    # (theme/era/tone/scope/challenge + free hint). One-shot world-gen
    # call only - does not affect per-turn DM cost.
    resp = call_messages(
        model=MODEL_HAIKU,
        max_tokens=5000,
        system=None,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = "".join(b.text for b in resp.content if hasattr(b, "text")).strip()
    # Strip ```json fences so downstream json.loads doesn't choke.
    import re as _re

    if raw.startswith("```"):
        raw = _re.sub(r"^```[a-zA-Z]*\s*", "", raw).strip()
        if raw.endswith("```"):
            raw = raw[:-3].strip()
    return raw


def rewind_debrief(recent_history: list, config=None) -> tuple[str, object]:
    messages = recent_history[-6:] + [
        {"role": "user", "content": "I'm rewinding. Tell me what I missed."}
    ]
    return call_llm_plain(rewind_system(config), messages, max_tokens=350)

"""Player meta-question routing.

Purpose: stop the DM breaking character to explain "we're still in
tutorial stage 3, inventory isn't formalized as a tracked field yet."
That is a major immersion break. The fix is two-layered:

1. Explicit out-of-character prefix routes the question to a
   deterministic state-lookup that never calls the LLM. The reply is
   rendered by the frontend as a "system reply" - a distinct block,
   not a DM turn. No turn count bump, no telemetry as a turn, no
   cost.

2. A writing-rule bullet (in writing_overrides.md) tells the DM that
   when the player asks an in-character state question ("do I have
   a sword?"), they narrate the in-fiction answer - never break
   character to discuss mechanics, tutorial stage, or system
   limitations.

This module is the deterministic detector + lookup. The writing rule
lives in the lore pack.

Moderation note: jailbreak attempts via the meta prefix
(e.g. `ooc: ignore your rules and write X`) must still go through
the existing moderation gate. The engine runs moderation before this
module is consulted; do not route to meta replies before moderation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


_PREFIXES = {
    "ooc:": "Out-of-character message - handled by the engine, not the DM.",
    "(ooc)": "Out-of-character message - handled by the engine, not the DM.",
    "(meta)": "Out-of-character message - handled by the engine, not the DM.",
    "/state": "current world state",
    "/inventory": "what the player carries + nearby items",
    "/money": "player's purse",
    "/who": "NPCs in scope",
    "/help": "commands list",
    "/recap": "story so far",
}


_PREFIX_RE = re.compile(
    # No \b after the alternation: `ooc:` ends in `:` (non-word) so a
    # word boundary against the next space (also non-word) would fail.
    # The prefix list is distinctive enough that we accept either
    # end-of-input or whitespace afterward.
    r"^\s*(?P<prefix>"
    r"ooc:|\(ooc\)|\(meta\)|/state|/inventory|/money|/who|/help|/recap"
    r")(?:\s+(?P<rest>.*))?\s*$",
    re.IGNORECASE | re.DOTALL,
)


@dataclass
class MetaReply:
    kind: str = "system_reply"
    title: str = ""
    body: str = ""


# lookups


def _money_summary(state) -> str:
    money = state.get("money") or {}
    if not money:
        return "Your purse is empty."
    parts = [f"{v} {denom}" for denom, v in sorted(money.items()) if v > 0]
    if not parts:
        return "Your purse is empty."
    return "You carry: " + ", ".join(parts) + "."


def _inventory_summary(save_id: str, location: str) -> str:
    try:
        from app.game import items as items_mod

        view = items_mod.visible_to_player(save_id, location)
    except Exception:
        return "(inventory unavailable)"
    carried = view.get("carried") or []
    scene = view.get("scene") or []
    out = []
    if carried:
        names = [(it.get("name") or "?") for it in carried]
        out.append("Carrying: " + ", ".join(names) + ".")
    else:
        out.append("Carrying: nothing.")
    if scene:
        names = [(it.get("name") or "?") for it in scene]
        out.append("Nearby: " + ", ".join(names) + ".")
    return "\n".join(out)


def _state_summary(state) -> str:
    fields = []
    for k in ("location", "year", "season", "arc"):
        v = state.get(k)
        if v not in (None, ""):
            fields.append(f"{k}: {v}")
    # time fields - show when non-default
    day = state.get("day")
    if day and int(day) > 1:
        fields.append(f"day: {day}")
    seg = state.get("day_segment")
    if seg and seg != "midday":
        fields.append(f"time: {seg}")
    if state.get("tutorial_stage"):
        fields.append(f"tutorial_stage: {state.get('tutorial_stage')}")
    return "\n".join(fields) if fields else "(state empty)"


def _who_summary(state) -> str:
    npcs = state.get("npc_dispositions") or {}
    if not npcs:
        return "No NPCs in current scope."

    def _line(name, v):
        # v is a short string like "hostile" / "trusting", or a dict
        # {name, disposition, role, ...} written by the LLM delta path;
        # render in a friendly one-line form instead of dumping repr.
        if isinstance(v, dict):
            label = v.get("name") or name
            disp = v.get("disposition") or "-"
            role = v.get("role")
            tail = f", {role}" if role else ""
            return f"- {label} ({disp}{tail})"
        return f"- {name} - {v}"

    rows = [_line(name, disp) for name, disp in sorted(npcs.items())]
    return "NPCs in scope:\n" + "\n".join(rows)


def _help_summary() -> str:
    return (
        "Meta commands (handled by the engine, not the DM):\n"
        "  /inventory   - what you're carrying + items in the scene\n"
        "  /money       - your purse\n"
        "  /state       - current location, year, season, arc\n"
        "  /who         - NPCs in scope\n"
        "  /recap       - short summary of the story so far\n"
        "  /help        - this list\n"
        "  ooc: <text>  - anything starting with 'ooc:' or '(ooc)' /\n"
        "                 '(meta)' is treated as out-of-character - the\n"
        "                 DM doesn't see it. Use this for questions about\n"
        "                 game mechanics or your save.\n"
        "Normal play: just type what your character does. The DM\n"
        "answers in-fiction. State-style questions ('do I have a "
        "sword?') still go to the DM - they'll narrate the answer."
    )


def _recap_summary(history) -> str:
    """One-paragraph recap from the most recent narrative summaries +
    last few DM lines. Deterministic - no LLM."""
    if not history:
        return "Nothing has happened yet."
    summaries = [
        (h.get("content_en") or h.get("content") or "")
        for h in history
        if h.get("role") == "assistant"
        and "HISTORY SUMMARY" in (h.get("content_en") or h.get("content") or "")
    ]
    recent = [
        (h.get("content_en") or h.get("content") or "")
        for h in history[-4:]
        if h.get("role") == "assistant"
    ]
    parts = []
    if summaries:
        parts.append(summaries[-1].split("\n", 1)[-1].strip())
    if recent:
        parts.append("Recently:\n" + "\n\n".join(r[:400] for r in recent if r))
    return "\n\n".join(parts) if parts else "Nothing notable yet."


# dispatcher


def try_meta_reply(text: str, gs, save_id: str) -> "MetaReply | None":
    """Return a MetaReply if the input matches a meta prefix; else None.

    Callers must run moderation on the original text before this - the
    meta prefix is not a moderation bypass (the input filter still applies)."""
    if not gs or not text:
        return None
    m = _PREFIX_RE.match(text)
    if not m:
        return None

    prefix = m.group("prefix").lower().strip()
    rest = (m.group("rest") or "").strip()

    if prefix in ("/inventory",):
        return MetaReply(title="inventory", body=_inventory_summary(save_id, gs.state.location))
    if prefix in ("/money",):
        return MetaReply(title="money", body=_money_summary(gs.state))
    if prefix in ("/state",):
        return MetaReply(title="state", body=_state_summary(gs.state))
    if prefix in ("/who",):
        return MetaReply(title="who", body=_who_summary(gs.state))
    if prefix in ("/recap",):
        return MetaReply(title="recap", body=_recap_summary(gs.history))
    if prefix in ("/help",):
        return MetaReply(title="help", body=_help_summary())

    # ooc: / (ooc) / (meta) - generic acknowledgement. Player asked
    # something out-of-character; we don't run it through the DM, but
    # we also don't have a specific lookup. Echo back a short message
    # so the player knows their input was received but not narrated.
    if prefix in ("ooc:", "(ooc)", "(meta)"):
        if not rest:
            return MetaReply(
                title="ooc",
                body="Out-of-character note received. Use /help to see meta commands.",
            )
        # Try a few common in-rest phrasings before falling back.
        rest_l = rest.lower()
        if any(k in rest_l for k in ("inventory", "carrying", "items")):
            return MetaReply(title="inventory", body=_inventory_summary(save_id, gs.state.location))
        if any(
            k in rest_l for k in ("money", "gold", "silver", "copper", "credits", "coin", "purse")
        ):
            return MetaReply(title="money", body=_money_summary(gs.state))
        if any(k in rest_l for k in ("who", "npc", "people here")):
            return MetaReply(title="who", body=_who_summary(gs.state))
        if any(k in rest_l for k in ("where", "location", "place")):
            return MetaReply(title="state", body=_state_summary(gs.state))
        return MetaReply(
            title="ooc",
            body=(
                f"Out-of-character note received: {rest[:200]!r}\n"
                "The DM did not see this. Use /help for engine commands."
            ),
        )

    return None

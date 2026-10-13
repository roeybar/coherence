"""World-systems schema helpers.

Every new field defaults to empty / "unknown" so legacy saves continue to
load without migration. Each helper either reads with a default or writes
back in the structured shape, and parsers tolerate both legacy (string-
list) and new (dict-list) entries where the field already existed.

Fields read here. They are fields in the pack's state_schema.json, so the
DM's state delta can write them; the engine itself writes world_rules
(engine.py) and verification-pending active_threats
(social_systems.maybe_record_bluff):

  - state.world_rules           player-declared world rules (scope-tagged) - widens state.self_restrictions semantics
                                without breaking the existing field.
  - state.production_techniques list[{name, description}]
  - state.player_conditions     list[{kind, summary, severity,
                                resolvable_via?}]
  - state.day_segment           enum dawn|morning|midday|afternoon|evening|
                                dusk|night|deep_night
  - state.pc_only_anomalies     list[{token, kind, trigger_pattern?}]
  - state.declined_hooks        list[{summary, declined_at_turn}]
  - state.active_quests         each entry str (legacy) or
                                {title, difficulty, summary?, risks?,
                                 reward_hint?, status, kind:explicit|implicit}
  - state.active_threats        each entry str (legacy) or
                                {actor_or_thing, position?, status,
                                 verification_pending?}

NPC-side additions live in npcs.py:

  - NPC.reveal_state            {saw_what:[str], believes_what:[str],
                                will_do_what:[str]}
  - NPC.bound_status            short flag string (e.g. "wrists bound";
                                observation only, never used to refuse
                                player action - see lore physicalisation
                                rule)

This module only owns the helpers + render blocks; engine.py wires them
into run_turn and the DM beat reads the rendered text.
"""

from __future__ import annotations

import re
from typing import Any, Optional


# enums


DAY_SEGMENTS = (
    "dawn",
    "morning",
    "midday",
    "afternoon",
    "evening",
    "dusk",
    "night",
    "deep_night",
)
QUEST_DIFFICULTIES = ("easy", "medium", "hard", "hard_plus")
QUEST_KINDS = ("explicit", "implicit")


# world rules


def parse_world_rule(player_input: str) -> Optional[dict]:
    """Detect a player-declared world-policy rule (e.g. 'no AI for 25
    years'). Distinct from physicalisation self-restrictions (those are
    parsed by physicalisation.parse_self_restriction). Returns
    {rule, snippet, scope:'world-policy'} on match, None otherwise.
    """
    if not player_input:
        return None
    m = _PATTERN_WORLD_RULE.search(player_input)
    if not m:
        return None
    start = max(0, player_input.rfind(".", 0, m.start()) + 1)
    end = player_input.find(".", m.end())
    if end < 0:
        end = len(player_input)
    snippet = player_input[start:end].strip()[:240]
    return {
        "rule": m.group(0).strip()[:120],
        "snippet": snippet,
        "scope": "world-policy",
    }


_PATTERN_WORLD_RULE = re.compile(
    r"\b(?:"
    # Bans / prohibitions at world scope: "no X[ X[ X]] for N years", etc.
    # The middle (\w+\s+){1,4} lets phrases like "no AI systems" or
    # "no large airships" match without unbounded backtracking.
    r"no\s+(?:\w+\s+){1,4}(?:for|until)\s+\d+\s+(?:years?|months?|weeks?|generations?)|"
    r"(?:is|are)\s+(?:banned|forbidden|outlawed|prohibited)|"
    r"i\s+(?:ban|forbid|outlaw|prohibit)\s+(?:all|every|the)|"
    r"from\s+now\s+on\s+(?:no\s+one|nobody|everyone|all)|"
    # Mandatory policy: "all X must Y", "10% global tax"
    r"all\s+\w+\s+(?:must|shall|will)\s+\w+|"
    r"\d+%\s+(?:global|world|planetary)\s+\w+|"
    # Decree-shape
    r"(?:by\s+(?:my|our)\s+decree|by\s+royal\s+order)|"
    r"this\s+is\s+(?:now\s+)?policy"
    r")\b",
    re.IGNORECASE,
)


def render_world_rules_block(state) -> str:
    """Compose the DM-beat block enumerating active player-declared
    world rules. Reads state.world_rules and state.self_restrictions
    (the player's self-imposed limits) so the DM sees the unified set.
    Returns "" if neither field is populated."""
    parts: list[str] = []

    rules = list(state.get("world_rules") or [])
    if rules:
        parts.append("PLAYER-DECLARED WORLD RULES (the world enforces these):")
        for r in rules[-6:]:
            quote = r.get("snippet") or r.get("rule") or ""
            scope = r.get("scope") or "world-policy"
            if quote:
                parts.append(f'  - [{scope}] "{quote}"')

    return "\n".join(parts)


# production techniques


def render_production_techniques_block(state) -> str:
    techs = list(state.get("production_techniques") or [])
    if not techs:
        return ""
    parts = [
        "PLAYER-ESTABLISHED BUILD TECHNIQUES (prefer these for new construction unless the player declares otherwise):"
    ]
    for t in techs[-5:]:
        nm = t.get("name") or ""
        desc = t.get("description") or ""
        if nm and desc:
            parts.append(f"  - {nm}: {desc}")
        elif nm:
            parts.append(f"  - {nm}")
    return "\n".join(parts)


# player conditions (soft channel - observation only)


def render_player_conditions_block(state) -> str:
    conditions = list(state.get("player_conditions") or [])
    if not conditions:
        return ""
    parts = [
        "PLAYER-SIDE SOFT CONDITIONS (frame, do not gate - the player can always act through them):"
    ]
    for c in conditions[-5:]:
        kind = c.get("kind") or "status"
        summ = c.get("summary") or ""
        sev = c.get("severity") or "mild"
        res = c.get("resolvable_via") or ""
        line = f"  - [{kind}/{sev}] {summ}"
        if res:
            line += f" (resolves via: {res})"
        parts.append(line)
    return "\n".join(parts)


# day segment


def get_day_segment(state) -> str:
    """Returns the current day_segment, defaulting to 'midday' when
    unset so legacy saves get a sensible value."""
    seg = state.get("day_segment") or "midday"
    return seg if seg in DAY_SEGMENTS else "midday"


def render_day_segment_block(state) -> str:
    seg = get_day_segment(state)
    pressures = {
        "dawn": "first light; the world is uncurling; sound carries far",
        "morning": "the day's work has started; markets and roads are busy",
        "midday": "sun high; heat presses; shadows are short",
        "afternoon": "long shadows lengthening; the day's tempo slows",
        "evening": "lamps are lit; the day's news settles into rumour",
        "dusk": "the world is between; doors that were open close; what hunts begins to move",
        "night": "the dark is owned by guards, drunks, and what does not sleep",
        "deep_night": "the still hours; whatever moves now is hiding from someone",
    }
    return f"TIME OF DAY: {seg} - {pressures.get(seg, '')}"


# PC-only sensory anomalies


def render_pc_only_anomalies_block(state) -> str:
    anomalies = list(state.get("pc_only_anomalies") or [])
    if not anomalies:
        return ""
    parts = [
        "PC-ONLY SENSORY ANOMALIES (only the player perceives these - NPCs deny or fail to notice when asked):"
    ]
    for a in anomalies[-5:]:
        tok = a.get("token") or ""
        knd = a.get("kind") or ""
        trg = a.get("trigger_pattern") or ""
        line = f"  - [{knd}] {tok}"
        if trg:
            line += f" (trigger: {trg})"
        parts.append(line)
    return "\n".join(parts)


# quest + threat objectification


def normalize_quest(entry: Any) -> dict:
    """Coerce a legacy string entry or a partial dict into the full
    structured shape. Missing fields get defaults."""
    if isinstance(entry, str):
        return {
            "title": entry.strip()[:180],
            "difficulty": "medium",
            "status": "active",
            "kind": "explicit",
        }
    if not isinstance(entry, dict):
        return {
            "title": str(entry)[:180],
            "difficulty": "medium",
            "status": "active",
            "kind": "explicit",
        }
    out = dict(entry)
    out["title"] = str(out.get("title") or "")[:180]
    diff = str(out.get("difficulty") or "medium").lower()
    out["difficulty"] = diff if diff in QUEST_DIFFICULTIES else "medium"
    out["status"] = str(out.get("status") or "active")[:40]
    kind = str(out.get("kind") or "explicit").lower()
    out["kind"] = kind if kind in QUEST_KINDS else "explicit"
    if out.get("summary"):
        out["summary"] = str(out["summary"])[:240]
    if out.get("risks"):
        out["risks"] = str(out["risks"])[:240]
    if out.get("reward_hint"):
        out["reward_hint"] = str(out["reward_hint"])[:240]
    return out


def normalize_threat(entry: Any) -> dict:
    """Coerce a legacy string entry or a partial dict into the full
    structured threat shape."""
    if isinstance(entry, str):
        return {
            "actor_or_thing": entry.strip()[:180],
            "status": "active",
            "verification_pending": False,
        }
    if not isinstance(entry, dict):
        return {
            "actor_or_thing": str(entry)[:180],
            "status": "active",
            "verification_pending": False,
        }
    out = dict(entry)
    out["actor_or_thing"] = str(out.get("actor_or_thing") or "")[:180]
    out["status"] = str(out.get("status") or "active")[:60]
    out["verification_pending"] = bool(out.get("verification_pending") or False)
    if out.get("position"):
        out["position"] = str(out["position"])[:80]
    return out


def quests_for_prompt(state) -> list[dict]:
    return [normalize_quest(q) for q in (state.get("active_quests") or [])]


def threats_for_prompt(state) -> list[dict]:
    return [normalize_threat(t) for t in (state.get("active_threats") or [])]


def render_quests_block(state) -> str:
    quests = quests_for_prompt(state)
    if not quests:
        return ""
    parts = ["ACTIVE QUESTS:"]
    for q in quests[-6:]:
        title = q.get("title") or ""
        diff = q.get("difficulty") or "medium"
        kind = q.get("kind") or "explicit"
        status = q.get("status") or "active"
        line = f"  - [{diff}/{kind}/{status}] {title}"
        if q.get("summary"):
            line += f" - {q['summary']}"
        parts.append(line)
        if q.get("risks") and diff in ("hard", "hard_plus"):
            parts.append(f"      risk: {q['risks']}")
    return "\n".join(parts)


def render_threats_block(state) -> str:
    threats = threats_for_prompt(state)
    if not threats:
        return ""
    parts = [
        "ACTIVE THREATS (passive pressure - ambient-mention the highest-priority one when relevant):"
    ]
    for t in threats[-6:]:
        actor = t.get("actor_or_thing") or ""
        status = t.get("status") or "active"
        pos = t.get("position") or ""
        line = f"  - {actor} - {status}"
        if pos:
            line += f" [{pos}]"
        if t.get("verification_pending"):
            line += (
                " [VERIFICATION PENDING - player claim that resolved a scene; may be challenged]"
            )
        parts.append(line)
    return "\n".join(parts)


def add_verification_pending_threat(state, claim_summary: str) -> None:
    """When a player makes an unverifiable claim that resolves a scene
    (a bluff), append it to active_threats with verification_pending=True
    so future turns can challenge it."""
    if not claim_summary:
        return
    threats = list(state.get("active_threats") or [])
    threats.append(
        {
            "actor_or_thing": claim_summary.strip()[:160],
            "status": "unverified player claim",
            "verification_pending": True,
        }
    )
    state.active_threats = threats[-20:]


# shape-drift protection for load-bearing structured entries


# Tokens we use to fingerprint a verification-pending threat across a
# delta replacement. The model often paraphrases the actor_or_thing
# string but keeps recognisable substrings; we use the first few
# meaningful words of the original claim to match.
def _claim_fingerprint(text: str) -> set[str]:
    if not text:
        return set()
    words = [w.lower().strip(".,!?\"'()[]") for w in text.split() if len(w) >= 4]
    return set(words[:8])  # first 8 meaningful words


def reapply_verification_pending(pre_state_dict: dict, state) -> int:
    """Post-DM-beat pass: the DM beat's state_delta often emits
    active_threats as a list of plain strings, which replaces our
    structured entries via apply_delta (lists are replaced wholesale).
    This helper reads the PRE-turn state for entries flagged
    verification_pending=True and restores their structured form to
    state.active_threats after the delta has been applied, merging
    any DM status updates from a matching string entry.

    Returns the count of entries restored or upgraded.

    Matching: we match a pre-turn structured bluff to a current
    string entry by checking whether they share >=2 meaningful words
    from the original claim (>=4 chars, stripped of punctuation).
    If matched, the structured entry's `status` is updated from the
    DM string (so we keep the model's narrative progression) but
    `actor_or_thing` and `verification_pending=True` are preserved.
    If unmatched, we still re-append the structured entry - the bluff
    survives even if the DM dropped it entirely.

    Idempotent: if the structured entry is already present in the
    current list, we leave it alone.
    """
    pre_threats = list(pre_state_dict.get("active_threats") or [])
    pre_bluffs = [t for t in pre_threats if isinstance(t, dict) and t.get("verification_pending")]
    if not pre_bluffs:
        return 0

    cur_threats = list(state.get("active_threats") or [])
    restored = 0

    for bluff in pre_bluffs:
        # Already present in structured form? Leave alone.
        if any(
            isinstance(t, dict)
            and t.get("verification_pending")
            and t.get("actor_or_thing") == bluff.get("actor_or_thing")
            for t in cur_threats
        ):
            continue

        # Try to match a current string entry that's a paraphrase of
        # our bluff and replace it with the merged structured form.
        bluff_fp = _claim_fingerprint(bluff.get("actor_or_thing") or "")
        merged = False
        for i, t in enumerate(cur_threats):
            if isinstance(t, dict):
                continue
            t_str = str(t)
            t_fp = _claim_fingerprint(t_str)
            if len(bluff_fp & t_fp) >= 2:
                cur_threats[i] = {
                    "actor_or_thing": bluff.get("actor_or_thing"),
                    "status": t_str[:200],
                    "verification_pending": True,
                }
                merged = True
                restored += 1
                break

        if not merged:
            # DM dropped the bluff entirely - re-append the original
            # structured form so it survives the turn.
            cur_threats.append(bluff)
            restored += 1

    if restored:
        state.active_threats = cur_threats[-20:]
    return restored


# unified DM-beat composition


# declined hooks (fade-only persistence)


def render_declined_hooks_block(state) -> str:
    hooks = state.get("declined_hooks") or []
    if not hooks:
        return ""
    parts = [
        "DECLINED HOOKS (the player walked away from these; FADE their "
        "sensory presence over subsequent turns - do NOT escalate volume, "
        "proximity, or repetition to pull them back. Hooks may resurface "
        "when the player choses to engage; they do not chase the player):",
    ]
    for h in hooks[-5:]:
        parts.append(f'  - T{h.get("declined_at_turn", "?")}  "{h.get("summary", "")}"')
    return "\n".join(parts)


def render_world_systems_block(state) -> str:
    """Compose all world-systems surface text into one block the DM beat
    appends to its system prompt. Sections that have no content are
    skipped, so the block stays empty when nothing is active."""
    from app.game.campaign_clock import render_clock_block

    sections = [
        render_clock_block(state),
        render_world_rules_block(state),
        render_quests_block(state),
        render_threats_block(state),
        render_player_conditions_block(state),
        render_production_techniques_block(state),
        render_pc_only_anomalies_block(state),
        render_declined_hooks_block(state),
        render_day_segment_block(state),
    ]
    nonempty = [s for s in sections if s]
    return "\n\n".join(nonempty)

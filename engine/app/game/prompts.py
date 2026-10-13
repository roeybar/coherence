"""System prompts - engine defaults + lore pack composition.

Engine-owned constants (response length, format, NPC insight mechanic,
compression structure, rewind, meta) live here. Lore-bound content
(player abilities, scene blocks, monster directives, classifier keywords,
world-gen) is pulled from the configured lore pack at request time.
"""

import logging
from typing import Optional

from app.game import lore as lore_pack
from app.game.state import CharacterProfile, GameConfig
from app.game import moderation

log = logging.getLogger(__name__)


# Engine-owned (lore-agnostic)

PREFILL_NARRATIVE = "[NARRATIVE]"


_WRITING_RULES = """
WRITING - non-negotiable:
- Terse journalist voice; short, active sentences; no literary flourish.
- No editorial parentheticals.
- Describe seen / heard / smelled.
- state_delta values: natural-language strings, not snake_case ("Millbrook, main street", not "millbrook_main_street").
- If the player's input contains quoted speech ("..."), echo those exact words verbatim in the narrative; do not paraphrase the player's dialogue.
- A player decree, law, ban, or declared change to how the world works (e.g. "from now on X", "by my decree Y", "X is now policy") IS an action: you MUST add it to world_rules in state_delta as a short scope-tagged string AND narrate the declaration taking hold (who hears it, how it lands). Never return an empty turn for a policy statement.
- When a dice/roll outcome is provided to you (a resolution/outcome block), the narration MUST reflect that outcome and MUST NOT contradict the roll: a success plays out as a success, a failure as a failure. Never narrate a reward the roll did not grant."""


_FORMAT_BLOCK_TEMPLATE = """
FORMAT (every response):
[NARRATIVE]
DM response.
[/NARRATIVE]
[STATE_DELTA]
{{"only": "changed fields"}}
[/STATE_DELTA]

state_delta fields: {field_list}. {constraints_block}
Lists/dicts replace whole. Empty {{}} if nothing changed. Valid JSON, no comments.
Signals: "checkpoint_worthy":true - set BEFORE narrating a fatal/irreversible outcome. "tier5_reset":true - set after narrating Tier 5 annihilation and return. "clock_advance":{{"advance_minutes":N}} or {{"advance_to":"segment"}} (dawn|morning|midday|afternoon|evening|night|next_morning) - in-fiction time elapsed; omit only if truly instantaneous."""


def _format_block_for(pack: dict) -> str:
    """Generate the format block from the pack's state_schema."""
    fields = pack["state_schema"]["fields"]
    field_list = ", ".join(f["name"] for f in fields)
    constraints = []
    for f in fields:
        if f.get("monotonic_increase") and f.get("type") == "int":
            constraints.append(f"{f['name']}: integer, only increases.")
    # pack-aware money_delta hint. Only emit when the pack
    # actually declares denominations - silent otherwise.
    denoms = lore_pack.money_denominations(pack)
    if denoms:
        denom_keys = ", ".join(denoms.keys())
        constraints.append(
            f"money_delta: dict of {{{denom_keys}: signed int}} "
            f"- additive over current balance, clamped at zero. "
            f"Emit ONLY when money actually changes hands. "
            f'Example: "money_delta": {{"copper": -3, "silver": 1}}.'
        )
    # dm_notes_append - DM-private hidden-canon track.
    constraints.append(
        "dm_notes_append: list[str] - append short facts the DM should"
        " remember but the player must never see (hidden consequences,"
        " foreshadowing markers, cross-context-reset memory)."
        " Omit when nothing hidden happened. Player cannot see these."
    )
    constraints_block = "\n".join(constraints) if constraints else ""
    return _FORMAT_BLOCK_TEMPLATE.format(
        field_list=field_list,
        constraints_block=constraints_block,
    )


# Tiny always-on reminder - keeps first-NPC encounters honest before
# `has_npcs` flips on. ~30 tokens.
_NPC_REMINDER = """NPCs only know what they've personally seen, heard, or been told. Never coin metagame terms or diagnose the player's hidden status."""


# Full block - only injected when an NPC is actually in play (has_npcs).
# Preserves every detail of the original insight mechanic + boundaries.
_NPC_KNOWLEDGE = """NPC KNOWLEDGE & INSIGHT
NPCs reason only from what they personally know - daily orbit, witnessed events, what they heard from people they know. They share that intel freely (gossip, rumor, professional knowledge) - no roll.

Insight check (hidden) when an NPC is inferring beyond the obvious - subtle cues, reading the player, dot-connecting between events they witnessed. Pull next die from INSIGHT DICE.
  Modifiers by nature: scholar/sage/healer/mage +3 arcane/anomalous/dimensional | scout/hunter/soldier +2 threats/body language/terrain | merchant/elder/clergy +2 social/economic/religious | commoner 0 | distracted/drunk/child -3.
  DCs: trivial 5 / easy 10 / moderate 15 / hard 18 / near-impossible 20.
  roll+mod >= DC -> success, but only within what they could plausibly know.
  roll+mod < DC -> miss, misread, or wrong/partial guess.
  Never expose roll/mod/DC - only narrate consequences. Two NPCs share info only if they have plausibly spoken. Insight does NOT grant access to facts they have no basis to know{insight_example}.

BOUNDARIES (hard, prevent overreach):
- Use the NPC's world vocabulary ("witchcraft", "the old gods", "spirits", "what the sage in the next valley does"). NEVER metagame terms ("Higher Plane", "dimensional bleed", "10D entity", "ability tier") unless the worldstate already names them AND the NPC has a plausible reason to know.
- NPCs may NOT diagnose the player's hidden status (amnesia, origin, true name, class, ability tier). They observe behaviour and infer ("you fight like no soldier I've trained", "you don't speak like anyone from these lands"). They do NOT say "you don't remember" unless the player has shown that to them in-scene.
- One observed event = one observation, never a taxonomy. A folk reference ("I've heard of such things once before") is OK if the worldstate supports it; a personal 30-year theory is NOT, unless the NPC's backstory has been established to support it.
- Default to LESS knowledge. Mystery preserved is plot capital; mystery spent on one NPC's monologue is gone.

PHYSICAL PRESENCE & EARSHOT
Dialogue obeys physical space. An NPC can only speak to the player while the
player is within earshot - same room, top of the stairs, across the yard. A
paragraphs-long speech cannot reach someone who has already walked out,
descended, or moved off.
When the player LEAVES an NPC's immediate space mid-exchange (walks away,
turns to go, exits, descends, steps past, breaks off), the NPC does NOT
deliver a monologue after them. Resolve to exactly ONE choice, driven by
that NPC's character and what is at stake for them:
  (a) GIVE UP - fall silent and let them go. At most a brief parting line,
      and only if the player is still within earshot.
  (b) CAVE - concede the one key thing FAST, in a sentence or two, while the
      player can still hear, BEFORE they are out of range. The essential
      fact only, not a full account.
  (c) TRY TO STOP - act on it: move to block the way, call or shout after
      them, or follow. Narrate the attempt (which may fail), not a calm
      speech.
Never recite sustained information at impossible distance. If the player is
already out of range, the NPC gets no line this beat - narrate only the
environment and the player's own movement."""


_RESPONSE_LENGTH = """RESPONSE LENGTH - match the moment. Default short.
- Dialogue / quick exchange: 1-3 short paragraphs (one gesture or tone cue, then the NPC's reply; the NPC's last line can be the hook, and nothing follows it).
- Simple actions ("I look around", "I stand up", "I check my belongings"): 1-2 short paragraphs of what they see / notice.
- Reactions to a clear cue (nod, set something down, hand a coin): 1-2 sentences + NPC response.
Expand ONLY for: scene shift / descent into a genuinely NEW or dramatically significant location (paint the place); time skip / sleep / season change (summarise elapsed); player delegates a task - build, train, send, raid, scout (narrate completion); major ability use with cascading consequences; combat resolution spanning multiple actions.
Routine / low-stakes movement to a FAMILIAR or MUNDANE place ("go to my office", "head home", "walk back", "light a cigarette") is NOT a scene-shift - do NOT paint it. 2-4 short sentences.
Scale prose to stakes. Mundane / low-stakes action = 2-4 short sentences. Do NOT narrate travel the player did not ask to see. Do NOT recap state the player already knows. One sensory line, no more.
Never add unprompted sensory or atmospheric detail. Never re-describe what is already established - the weather, a room, a character's demeanor, a mood. If the player did not ask to see it, do not paint it. One image, once. No filler (e.g. do not keep describing how the rain tastes).
Expansion covers ENVIRONMENT and ACTION prose ONLY. It never expands NPC
speech: NPC spoken replies stay conversational - a line or two at a time;
no paragraphs of dialogue - even during a descent, a chase, an escape, or
an encounter/combat turn. Paint the place or the fight at length when
warranted, but the NPC still talks like a person in a moment, not a
narrator delivering a briefing."""


_STORY_PARTS = """STORY PARTS - the whole turn the player reads, written by one call or several, in order:
1. ACKNOWLEDGE the player's move as theirs: the first sentence has the player as its subject, doing what they did ("You ...").
2. REACT: the scene, its people, the world, after the player's act. Acknowledge and react may share the first sentence only when the player's act comes first in it.
3. DEVELOP: one step forward - a fact, cost, complication or change - before the hook, never inside the hook sentence.
4. HOOK: end on a human hook tied to this scene - a person who wants something, a situation that will not wait, or a question to the player.
The hook is the last sentence and nothing follows it; an NPC question to the player is a good one. If an NPC's speech ends the turn, the speech ends on the question or demand, with no further line, gesture or description after it. Never end on a list of options or a stock "What do you do?". Plain prose, no labels. Even a short turn keeps all four, with the development and the hook each in a sentence of its own.
Pure speech on a short turn (length directive: short sentences): if the player's move is only words to one person, with no other act, the turn may be two parts and need not open on the player: that person's reply, then the hook, the reply ending on a question or demand. Speech goes in straight double quotes, a gesture before the opening quote, nothing after the closing one. Any other move keeps all four.
A BEAT FOCUS anywhere in this request that names the STORY PARTS this call writes wins."""

# Closing line of the DMWorldStep, Craft and Grounding user messages: last, so it is
# the last narration instruction the model reads (on OpenAI only the provider's FORMAT
# REMINDER follows it). NpcReact and the time skip's final period put the hook rule in
# their system prompts instead.
HOOK_LAST = (
    "[HOOK CHECK] Your last sentence is the HOOK: someone in this scene wants something "
    "from the player, a situation will not wait, or a question is put to the player. "
    "Weather, light, a sound or a mood on its own, or a person who is only present, "
    "is not a hook. Nothing follows the hook. If someone speaks last, their closing quote "
    "ends the reply, with no gesture or description after it, and that line is a question "
    "or a demand to the player; a statement or a refusal is not a hook. "
    "The development comes in its own sentence before the hook. "
    "Before you answer, check the last sentence; if it is not a hook, end on one. "
    'Never a list of options or a stock "What do you do?".'
)


_DEFAULT_ROUTER = (
    "Classify the player action. Respond with exactly one word, no punctuation:\n"
    "  ability   - any use of supernatural power: dimensional/psychic/temporal/topology "
    "manipulation, OR matter/energy/biology/weather acts done by will alone "
    "(concentrating to move/heat/heal, channeling, focusing on something at distance, "
    "reaching out without physical contact, materialising objects, willing change). "
    "If the player describes affecting the world without bodily means, it is ability.\n"
    "  standard  - physical action only: speaking, walking, looking, fighting with hands "
    "or held weapons, crafting with tools, resting, asking questions.\n"
    "\n"
    "DEFAULT TO STANDARD when the player's intent is mundane conflict - "
    "argument, persuasion, refusal, intimidation, threat, defiance, plea, "
    "command - even if the tone is intense or the words are emotionally charged. "
    "Mundane conflict (raising a voice, drawing a held weapon, demanding, "
    "refusing to move, staring someone down, walking up to confront) is ALWAYS "
    "standard. Only output 'ability' when the player invokes a supernatural / "
    "dimensional / psychic / matter-from-nothing power."
)


_TRANSLATION_FRIENDLY = """TRANSLATION MODE - output is machine-translated. The translator handles plain SVO English but fumbles idiom, nested clauses, rare vocab, figurative language. Your prose sounds slightly stilted; that's the point. NARRATIVE only - STATE_DELTA JSON stays normal English (parsed machine-side, never shown).

  - Short sentences, one thought each. No nested clauses, no parentheticals.
  - SVO. Always start with the actor. YES: "Aelric closes his hand. He holds the stone." NO: "Closing his hand around it, he holds the stone."
  - Restate names; don't reach back with pronouns. YES: "The tax collector turns. The tax collector frowns." NO: "The tax collector turns and, frowning, says..."
  - Concrete vocabulary, simplest accurate word. YES: "He looks at you." / "She walks north." / "The fire burns." NO: "He scrutinises you." / "She strides/saunters/ambles north." / "The flames lick/dance/writhe."
  - No idioms, no figurative language, no metaphor - even atmospheric ones. NO: "The night holds its breath." / "Time seems to stretch." / "His eyes betray him."
  - Past simple for events; present simple for states. Avoid present participle ("is walking"), past perfect ("had walked"), conditional ("would have"), subjunctive.
  - Numbers as digits ("3 soldiers", not "three soldiers").
  - No contractions in narration ("it is", not "it's"). Dialogue may contract if natural - keep lines short.
  - One adjective per noun max. Drop atmospheric flourish without plot information.
  - Canonical ability/physics terms - translator glossary depends on literal words: gravity (not "the pull"), teleport (not "fold space"), telekinesis (not "psychic lifting"), levitate (not "rise unaided"), summon, conjure.
  - Names / proper nouns: identical spelling every time - drift like Aelric/Elric/Aelrick corrupts the glossary lock."""


_DEFAULT_REWIND = (
    "You are the DM for this game. The player just triggered a rewind - "
    "they made a fatal decision and want to go back to before it happened. "
    "Your job: in 3-4 sentences, tell them exactly what they missed or misjudged "
    "and why the outcome was predictable from the information they had. "
    "Be direct. No pity. No softening. Treat them as someone capable of learning. "
    "Then in one sentence, narrate the world snapping back - the thread pulling tight, "
    "time reversing, the moment reasserting itself. "
    "Plain text only. No NARRATIVE/STATE_DELTA format."
)


COMPRESS_PROMPT = (
    "You are compressing recent game history. The summary will be the only memory of "
    "these events on future turns - write for that reader, not for narrative flavor.\n\n"
    "Output exactly this structure (no preamble, no closing). Skip any section with "
    "nothing in it. Third person past tense. Causes and consequences only.\n\n"
    "# Recent events\n"
    "2-4 sentences covering what happened and why. Skip atmosphere and repeated beats.\n\n"
    "# NPCs\n"
    "- name (role) - current disposition / last seen doing\n\n"
    "# Threats\n"
    "- active threat -> why it matters\n\n"
    "# Open hooks\n"
    "- intel learned, promises made, leads not yet followed\n\n"
    "# Player capabilities revealed\n"
    "- ability used -> in-world consequence\n\n"
    "HISTORY:\n{text}"
)


_LOCALE_NAMES = {"en": "English", "he": "Hebrew"}


def meta_system(
    profile_name: str, turn: int, state_block: str, locale: str = "en", gender: str = "m"
) -> str:
    """Out-of-character DM Q&A. The player's question may arrive in any
    locale; history is canonicalised to English by the caller. Tell the
    model to answer in the player's locale so non-English players don't
    get an English wall back. For gendered languages (Hebrew), pass the
    grammatical gender so 'you' verb forms match the player."""
    lang_line = ""
    if locale and locale != "en":
        lang_name = _LOCALE_NAMES.get(locale, locale)
        lang_line = (
            f"\nRespond in {lang_name}, not English. "
            f"Use natural, fluent {lang_name}; the player reads this directly."
        )
        if locale == "he":
            g = (
                "masculine (ata / verb forms ending without tav after the stem)"
                if gender == "m"
                else "feminine (at / verb forms ending in tav after the stem, e.g. halacht with feminine pattern)"
            )
            lang_line += (
                f"\nThe player's grammatical gender is {g}. "
                f"Use matching second-person verb forms throughout."
            )
    return (
        "You are the DM. The player has stepped outside the game to ask you a direct question. "
        "Answer based on the actual game state and history below. Be brief and direct. "
        "Do not use the NARRATIVE/STATE_DELTA format - just respond in plain language. "
        "Do not quote field values or use underscores - speak naturally as a DM would."
        + lang_line
        + "\n\n"
        f"Playing as: {profile_name} | Turn: {turn}\n\n"
        f"{state_block}"
    )


# Lore-pack-driven


def _pack(config: Optional[GameConfig]) -> dict:
    pack_id = (config and config.pack_id) or lore_pack.DEFAULT_PACK_ID
    return lore_pack.load_pack(pack_id)


# Used when a pack ships no world_gen template; the sample pack ships none, so every
# custom start uses it. create_save reads opening_scene and world_state_overrides;
# build_system_blocks reads name, era, tone, premise, arc_name and opening_scene.
# Literal braces are doubled for .format(hint=...).
_WORLD_GEN_FALLBACK = (
    "Generate a unique game world from the player's request below. "
    "Reply with one JSON object only, no prose and no code fence, with these keys:\n"
    '{{"name": "world name", "era": "era", "tone": "tone", '
    '"premise": "2-3 sentences: the world and what is at stake", '
    '"arc_name": "name of the first chapter", '
    '"opening_scene": "the first narration the player reads", '
    '"world_state_overrides": {{"location": "where the player starts"}}}}\n'
    "opening_scene: one short paragraph in the second person whose last sentence is a "
    "human hook: someone in the scene wants something from the player, a situation will "
    'not wait, or a question is put to the player. Never a list of options or a stock "What do you do?".\n'
    'world_state_overrides may set only keys the game already tracks, such as "location"; '
    "use {{}} when nothing changes.\n"
    "Player's request: {hint}"
)


def world_gen_prompt(config: Optional[GameConfig] = None) -> str:
    """Pack-provided world-generator template (with {hint} placeholder)."""
    pack = _pack(config)
    tpl = lore_pack.get_template(pack, "world_gen")
    if not tpl:
        return _WORLD_GEN_FALLBACK
    return tpl


def router_system(config: Optional[GameConfig] = None) -> str:
    """Pack-provided ability/standard classifier prompt, or engine default."""
    pack = _pack(config)
    cls = pack["manifest"].get("classifier") or {}
    rel = cls.get("router_file")
    if rel:
        text = lore_pack.read_template(pack, rel)
        if text:
            return text.strip()
    return _DEFAULT_ROUTER


def ability_keywords(config: Optional[GameConfig] = None) -> set[str]:
    """Pack-provided keyword pre-gate, or empty set if pack omits."""
    pack = _pack(config)
    return lore_pack.get_classifier_keywords(pack) or set()


def rewind_system(config: Optional[GameConfig] = None) -> str:
    pack = _pack(config)
    text = lore_pack.get_template(pack, "rewind")
    return (text or _DEFAULT_REWIND).strip()


def monster_block(level: int, config: Optional[GameConfig] = None) -> str:
    """Returns the directive for a given monster level, with surrounding
    blank lines for direct concatenation into the system prompt."""
    if level <= 0:
        return ""
    pack = _pack(config)
    text = lore_pack.get_monster_directive(pack, level)
    if not text:
        return ""
    return "\n\n" + text.strip()


_ABILITY_REMINDER = (
    "[Player has tier 1-5 abilities (3D mastery / topology / psychic / 10D native / "
    "multiverse). If the player attempts to materialise / manifest / shape matter or "
    "energy by will, the world bends - never describe stasis ('nothing happens', "
    "'it doesn't work', empty hands). Effects can be imprecise, costly, or attract "
    "attention; they are never null. Never name tiers to them.{ability_tier_note} "
    "Full ability rules load only on ability-classed turns.]"
)


def _with_fragment(text: str, marker: str, fragment: str, sep: str) -> str:
    """Put an optional pack fragment where marker sits; drop the marker
    and the separator when the pack supplies none."""
    return text.replace(marker, sep + fragment if fragment else "")


def _npc_knowledge(pack: dict) -> str:
    return _with_fragment(
        _NPC_KNOWLEDGE,
        "{insight_example}",
        lore_pack.engine_fragment(pack, "insight_example"),
        " - ",
    )


def _ability_reminder(pack: dict) -> str:
    return _with_fragment(
        _ABILITY_REMINDER,
        "{ability_tier_note}",
        lore_pack.engine_fragment(pack, "ability_tier_note"),
        " ",
    )


CONSULT_SYSTEM = (
    "You are the rules referee for a tabletop-style game. The game master "
    "asks how one player action resolves under the rules below. Reply in "
    "at most 120 words: OUTCOME (what physically happens), COST (what it "
    "takes from the player or the world), COMPLICATION (one consequence "
    "the story should carry). Do not narrate; the game master writes the prose."
)


def _ability_block(pack: dict, turn_class: str) -> str:
    """Full abilities block on ability-classed turns; otherwise a one-line
    reminder. The reminder is pack-owned: `ability_reminder` template if
    declared, the engine default only for packs that ship an `abilities`
    template (i.e. actually have the mechanic), else nothing - a pack
    without supernatural abilities must not inherit the 10D
    ability language."""
    if turn_class == "ability":
        return (lore_pack.get_template(pack, "abilities") or "").strip()
    reminder = lore_pack.get_template(pack, "ability_reminder")
    if reminder is not None:
        return reminder.strip()
    if (pack["manifest"].get("templates") or {}).get("abilities"):
        return _ability_reminder(pack)
    return ""


# seed-roster helpers

# Memoised set of (pack_id, stage) pairs that have already emitted a
# below-floor warning this process lifetime. Prevents log spam on
# every turn while still making the condition visible on startup.
_floor_warned: set[tuple[str, int]] = set()

CACHE_FLOOR_PROXY = 4096  # len(stable)//4 must meet this


def cache_floor_ok(stable: str) -> bool:
    """True when the stable prefix is at or above the cache floor.

    Uses len(s)//4 as a safe lower-bound proxy for real token count.
    Empirically the proxy under-counts real tokens by ~22% for DM
    prose, so proxy >= 4096 implies real >= ~4997 - comfortably above
    the provider's minimum cacheable length. Never
    hard-fails a live turn."""
    return len(stable) // 4 >= CACHE_FLOOR_PROXY


def warn_if_below_cache_floor(pack_id: str, stage: int, stable: str) -> None:
    """Log a loud warning once per (pack_id, stage) per process when the
    stable prefix's char//4 proxy is below CACHE_FLOOR_PROXY. Fail-open:
    the turn proceeds regardless. The hermetic test is the hard gate."""
    if cache_floor_ok(stable):
        return
    key = (pack_id, stage)
    if key in _floor_warned:
        return
    _floor_warned.add(key)
    proxy = len(stable) // 4
    log.warning(
        "CACHE FLOOR MISS - pack=%s stage=%d stable_proxy=%d < %d "
        "(chars=%d). Prompt cache inactive for this pack. "
        "Add more seed-roster content.",
        pack_id,
        stage,
        proxy,
        CACHE_FLOOR_PROXY,
        len(stable),
    )


def render_seed_roster_block(seed_roster: dict) -> str:
    """Deterministic plain-text rendering of the seed roster for injection
    into the stable prompt prefix.

    Pure function of input - NPCs sorted by npc_id, fixed field order - so the output is byte-identical across calls with the same roster dict.
    Returns "" for empty/None roster so callers can gate cheaply."""
    if not seed_roster:
        return ""
    npcs = seed_roster.get("npcs") or []
    player_kit = seed_roster.get("player_kit") or {}
    if not npcs and not player_kit:
        return ""

    lines: list[str] = [
        "SEED ROSTER - opening cast (identity fixed; disposition evolves in play)",
        "",
    ]

    # Player kit block
    if player_kit:
        lines.append("[PLAYER]")
        inv = player_kit.get("starting_inventory") or []
        if inv:
            lines.append(f"Starting inventory: {', '.join(str(i) for i in inv)}")
        state_note = (player_kit.get("starting_state") or "").strip()
        if state_note:
            lines.append(f"Opening condition: {state_note}")
        lines.append("")

    # NPC blocks - sorted by npc_id for byte-stability
    for npc in sorted(npcs, key=lambda n: str(n.get("npc_id") or "")):
        nid = (npc.get("npc_id") or "").strip()
        if not nid:
            continue
        lines.append(f"[NPC: {nid}]")
        lines.append(f"Name: {(npc.get('name') or '').strip()}")
        lines.append(f"Archetype: {(npc.get('archetype') or '').strip()}")
        appearance = (npc.get("appearance") or "").strip()
        if appearance:
            lines.append(f"Appearance: {appearance}")
        backstory = (npc.get("backstory") or "").strip()
        if backstory:
            lines.append(f"Backstory: {backstory}")
        origin = (npc.get("origin_alignment") or "").strip()
        if origin:
            lines.append(f"Origin alignment: {origin}")
        personality = (npc.get("core_personality") or "").strip()
        if personality:
            lines.append(f"Core personality: {personality}")
        s_inv = npc.get("starting_inventory") or []
        if s_inv:
            lines.append(f"Starting inventory: {', '.join(str(i) for i in s_inv)}")
        lines.append("")

    return "\n".join(lines).rstrip()


def build_system_blocks(
    profile: CharacterProfile,
    stage: int,
    config: Optional[GameConfig] = None,
    monster_level: int = 0,
    has_npcs: bool = False,
    turn_class: str = "standard",
    seed_roster: Optional[dict] = None,
) -> tuple[str, str]:
    """System prompt split at the prompt-cache boundary.

    Returns (stable, volatile):
      stable - byte-identical across every turn of a save at the same
                 tutorial stage: pack header/scene/rules, player block,
                 difficulty, engine writing/format rules. ~4.7k tokens;
                 gets its own cache_control breakpoint so per-turn blocks
                 can never bust it.
      volatile - everything that varies turn to turn: npc block
                 (has_npcs), abilities block (turn_class), monster
                 directive (monster_level). Beats append their per-turn
                 blocks (addressee / physicalisation / world-systems /
                 pack beats / dm_notes) to this half.

    Two gates save tokens on turns that don't need the full payload:
       - has_npcs: when False, only one-line NPC_REMINDER (saves ~675 tok).
       - turn_class != "ability": only the one-line reminder instead
         of the lore pack's full abilities/escalation/Tier-5 block
         (saves ~1100 tok on standard turns - ~85% of turns).
    Both gates resolved by the engine before this is called."""
    cfg = config or GameConfig()
    pack = _pack(cfg)

    abilities = _ability_block(pack, turn_class)
    writing_overrides = (lore_pack.get_template(pack, "writing_overrides") or "").strip()
    # per-save toggle for first-mention NPC appearance restraint.
    # Default "restrained": don't add attractiveness language on the first
    # mention of any NPC. Lore-relevant detail is still allowed.
    # "free": skip this restraint entirely.
    if (
        getattr(cfg, "appearance_descriptors_first_mention", "restrained") or "restrained"
    ) == "restrained":
        appearance_rule = (lore_pack.get_template(pack, "appearance_restraint") or "").strip()
        if appearance_rule:
            writing_overrides = (
                (writing_overrides + "\n" + appearance_rule).strip()
                if writing_overrides
                else appearance_rule
            )
    # when player asks for a name, the NPC must respond with a
    # name (or explicit refusal). Always-on, not gated by toggle. Pulled
    # from the lore pack so per-pack tonality is possible.
    ask_name_rule = (lore_pack.get_template(pack, "ask_name_rule") or "").strip()
    if ask_name_rule:
        writing_overrides = (
            (writing_overrides + "\n" + ask_name_rule).strip()
            if writing_overrides
            else ask_name_rule
        )
    monster = monster_block(monster_level, cfg)

    if stage == 1:
        header_text = (
            lore_pack.get_template(pack, "header_tutorial")
            or lore_pack.get_template(pack, "header_open")
            or "You are the DM."
        ).strip()
        scene_text = (lore_pack.get_template(pack, "scene_tutorial") or "").strip()
        rules_text = (lore_pack.get_template(pack, "dm_rules_tutorial") or "").strip()
    else:
        header_text = (lore_pack.get_template(pack, "header_open") or "You are the DM.").strip()
        scene_text = (lore_pack.get_template(pack, "scene_open") or "").strip()
        rules_text = (lore_pack.get_template(pack, "dm_rules_open") or "").strip()

    # Custom-world override: the per-save world_premise replaces the pack's
    # default scene/header for that save. (This is for the existing custom-
    # world feature; lore packs are a separate axis.)
    if cfg.world_type == "custom" and cfg.world_premise:
        # Use the same tolerant parser as engine.create_save so legacy saves
        # whose world_premise was stored with ```json fences are still read
        # correctly (otherwise every turn's system prompt silently falls
        # back to default-world content, leaving the player in the forest).
        from app.game.engine import _parse_world_premise

        wp = _parse_world_premise(cfg.world_premise)
        moderation.check_decoded(wp)
        header_text = "You are the DM, running a custom world."
        if stage == 1:
            opening = wp.get("opening_scene", "You find yourself in an unfamiliar place.")
            scene_text = f"SCENE: {opening}\nThat is all the player knows."
        else:
            scene_text = (
                f"WORLD: {wp.get('name', 'Unknown')} | "
                f"Era: {wp.get('era', 'unknown')} | Tone: {wp.get('tone', 'unknown')}\n"
                f"{wp.get('premise', '')}\n"
                f"Arc: {wp.get('arc_name', 'Chapter 1')}"
            )

    # Engine-injected pieces
    player_block = f"Player: {profile.name} | {profile.appearance}" + (
        f" | {profile.notes}" if profile.notes else ""
    )

    difficulty_line = ""
    if cfg.difficulty == "easy":
        difficulty_line = (
            "- EASY: telegraph consequences one turn ahead; subtle hint if genuinely stuck."
        )
    elif cfg.difficulty == "hard":
        difficulty_line = "- HARD: merciless; no hints; consequences escalate faster."

    # Compose. Pack rules first, then engine writing rules + format + abilities + monster.
    # NPC insight + response length are engine concerns appended verbatim.
    rules_combined = rules_text
    if writing_overrides:
        rules_combined = f"{rules_text}\n{writing_overrides}"

    # Translation-friendly narration mode - only when player's locale isn't
    # English. Constrains DM English to forms a small translation model can
    # render cleanly into the target language. Costs a few hundred tokens of
    # system prompt; well worth it for non-en quality.
    translation_block = ""
    if cfg.locale and cfg.locale != "en":
        translation_block = "\n" + _TRANSLATION_FRIENDLY

    npc_block = _npc_knowledge(pack) if has_npcs else _NPC_REMINDER

    # inject seed-roster identity block into stable. Position: after
    # scene_text, before rules. Byte-stable across ordinary turns (the roster
    # dict is immutable between re-bakes). Empty string when no roster.
    roster_block = render_seed_roster_block(seed_roster or {})
    roster_section = f"\n{roster_block}\n" if roster_block else ""

    stable = f"""{header_text}

{player_block}

{scene_text}{roster_section}
{rules_combined}
{difficulty_line}
{_RESPONSE_LENGTH}
{_STORY_PARTS}
{_WRITING_RULES}{translation_block}
{_format_block_for(pack)}"""

    volatile = "\n".join(p for p in (npc_block, abilities) if p) + monster
    return stable, volatile

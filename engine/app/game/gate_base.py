"""Common base for turn-gate exceptions.

A "gate" is a turn that stops early to ask the player something (dice roll,
craft roll, addressee, escalation consent, world-rule shift, ability intent,
reframe) or to refuse it (moderation). Gates are not errors: the request
returns 200 with a gate payload (or a clean 451). The turn is not played; the
API records the waiting input in the save (_store_pending_input in api/game.py).

Why this class exists - every gate is one exception type, so run_turn treats
a new gate like the others without a list of exceptions to update. Aux rows
gathered before a gate fires are dropped with it, so the save's token counts
cover completed turns only.

Deliberately not a catch-all: generic exceptions (real 500s) are not
caught here. Model calls are made on the player's own key, so a provider
call that completed is charged to that key by the provider even if the
turn later fails or stops at a gate.
Keep this module dependency-free so any gate module can import it.
"""


class TurnGate(Exception):
    """Base for every turn-gate exception. Subclasses need no extra wiring."""


def record_aux_call(aux_llm_rows, row_id: str, model: str, usage) -> None:
    """Append one aux-LLM row with the token counts of a gate's own call.

    A gate's DC-assessment call completes just before the gate raises, and
    the player's provider charges for it like any other call.

    No-ops when aux_llm_rows is None so the gate helpers stay callable from
    tests and tools.
    """
    if aux_llm_rows is None or usage is None:
        return
    t_in = getattr(usage, "input_tokens", 0) or 0
    t_out = getattr(usage, "output_tokens", 0) or 0
    c_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    c_creat = getattr(usage, "cache_creation_input_tokens", 0) or 0
    aux_llm_rows.append(
        {
            "id": row_id,
            "model": model,
            "tokens_in": t_in,
            "tokens_out": t_out,
            "cache_read": c_read,
            "cache_creation": c_creat,
        }
    )

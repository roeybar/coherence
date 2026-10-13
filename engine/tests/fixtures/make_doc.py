"""A realistic save doc of n turns, for size and round-trip checks."""

import base64

from app.game.state import UNDO_CAP, GameSave, WorldState, save_to_doc

VEC = base64.b64encode(bytes(768)).decode()


def make_doc(n_turns: int) -> dict:
    gs = GameSave(state=WorldState(location="Test room"))
    gs.config.pack_id = "testpack"
    history = [{"role": "assistant", "content_en": "o" * 1500}]
    for t in range(1, n_turns + 1):
        if t > n_turns - UNDO_CAP:
            gs.undo_stack.append(
                {"state": gs.state.to_dict(), "history": list(history), "turn": t - 1}
            )
        history += [
            {"role": "user", "content_en": "u" * 200},
            {"role": "assistant", "content_en": "a" * 1500},
        ]
    gs.history, gs.turn = history, n_turns
    doc = save_to_doc(gs)
    doc["items"] = []
    doc["memory"] = [
        {"turn": t, "role": r, "text": "m" * 200, "v": VEC}
        for t in range(n_turns)
        for r in ("user", "assistant")
    ]
    doc["physics_cache"] = [
        {"_id": "%032x" % i, "normalized_wish": "w%d" % i, "v": VEC, "narrative_hint": "h" * 200}
        for i in range(64)
    ]
    return doc

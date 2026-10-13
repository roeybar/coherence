"""Print the demo save file: a new neon_rain game made with the fake provider.

Run inside engine-test: python -m tests.make_demo_save > examples/demo-save.json
"""

import json
import os
import sys
import uuid

BODY = {
    "name": "Neon Rain",
    "profile": {"name": "Ash", "notes": ""},
    "config": {"pack_id": "neon_rain", "one_memory": ""},
}


def build() -> dict:
    os.environ["ENGINE_FAKE_PROVIDER"] = "1"
    from fastapi.testclient import TestClient
    from app.main import build_app

    client = TestClient(build_app("hosted"))
    headers = {
        "X-Provider": "anthropic",
        "X-Provider-Key": "demo-not-a-key",
        "X-Coherence-Session": str(uuid.uuid4()),
    }
    r = client.post("/api/game/new", json=BODY, headers=headers)
    r.raise_for_status()
    return {"format": "coherence-save", "v": 1, "save": r.json()["save"]}


if __name__ == "__main__":
    json.dump(build(), sys.stdout, indent=1, sort_keys=True, ensure_ascii=True)
    sys.stdout.write("\n")

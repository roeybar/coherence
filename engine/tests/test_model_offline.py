"""The engine image loads its baked embedding model with the model hub offline."""

import os
import socket

from app.game import memory


def _refuse(*args, **kwargs):
    raise OSError(f"network call refused: {args!r} {kwargs!r}")


def test_image_sets_hub_offline():
    from huggingface_hub import constants

    assert os.environ.get("HF_HUB_OFFLINE") == "1"
    assert constants.HF_HUB_OFFLINE
    print("PASS the image sets HF_HUB_OFFLINE=1 and the hub client reads it")


def test_baked_model_embeds_with_network_refused(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _refuse)
    monkeypatch.setattr(socket, "create_connection", _refuse)
    monkeypatch.setattr(socket.socket, "connect", _refuse)
    monkeypatch.setattr(memory, "_embedder", None)
    for text in ("x", "a red kite over the harbour", "rain " * 600):
        assert len(memory._embed(text)) == memory.VECTOR_SIZE
    print("PASS the baked model loads and embeds with every network call refused")

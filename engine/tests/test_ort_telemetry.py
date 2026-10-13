import onnxruntime

from app.game import memory


def test_telemetry_disabled_before_session(monkeypatch):
    calls = []
    monkeypatch.setattr(
        onnxruntime, "disable_telemetry_events", lambda: calls.append("off"), raising=False
    )
    monkeypatch.setattr(memory, "TextEmbedding", lambda name: calls.append("session"))
    monkeypatch.setattr(memory, "_embedder", None)
    memory._embed_model()
    assert calls == ["off", "session"]

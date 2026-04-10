"""Tests for StdinHitlHandler."""
from types import SimpleNamespace

from orchestrator.hitl import StdinHitlHandler


def _make_event(tool_use_id="tu_123", tool_name="Write", raw_input=None):
    return SimpleNamespace(
        tool_use_id=tool_use_id,
        name=tool_name,
        input=raw_input or {"file_path": "/work/foo.md", "content": "hi"},
    )


def test_approves_when_user_answers_yes(monkeypatch, capsys):
    calls = []

    def fake_send(tool_use_id, approved, reason):
        calls.append((tool_use_id, approved, reason))

    answers = iter(["y", "looks fine"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))

    handler = StdinHitlHandler(send_response=fake_send)
    handler.handle(_make_event())

    assert calls == [("tu_123", True, "looks fine")]
    out = capsys.readouterr().out
    assert "HITL APPROVAL REQUESTED" in out
    assert "Write" in out


def test_denies_when_user_answers_no(monkeypatch):
    calls = []
    answers = iter(["n", "too risky"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))

    handler = StdinHitlHandler(send_response=lambda *a: calls.append(a))
    handler.handle(_make_event(tool_use_id="tu_999"))

    assert calls == [("tu_999", False, "too risky")]


def test_reprompts_on_invalid_answer(monkeypatch):
    calls = []
    answers = iter(["huh?", "y", ""])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))

    handler = StdinHitlHandler(send_response=lambda *a: calls.append(a))
    handler.handle(_make_event())

    assert calls == [("tu_123", True, "")]


def test_eof_denies(monkeypatch):
    calls = []

    def raises_eof(_prompt=""):
        raise EOFError()

    monkeypatch.setattr("builtins.input", raises_eof)
    handler = StdinHitlHandler(send_response=lambda *a: calls.append(a))
    handler.handle(_make_event())

    assert calls == [("tu_123", False, "stdin closed")]

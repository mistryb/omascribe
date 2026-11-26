"""Tests for the Claude Code (headless `claude -p`) summarizer."""

import json
import stat

import pytest

from omascribe.ai_summarizer import ClaudeCodeSummarizer
from omascribe.config import AppConfig, validate_config
from omascribe.note_maker import CLOUD_PROVIDERS, NoteMaker

RESPONSE = """TITLE: Q3 "Inventory" Sync

OVERVIEW:
The team reviewed inventory reporting gaps.

KEY POINTS:
- Inventory job is still not running

ACTION ITEMS:
- Priya to fix the inventory job by Friday

DECISIONS:
- Buy-side contracts keep formula pricing

PARTICIPANTS:
Priya, Sam

DISCUSSION:
#### Inventory

The nightly job has failed since Monday.

- Root cause unknown
"""


def _fake_claude(tmp_path, payload, exit_code=0):
    """A stand-in `claude` that records its argv/env/stdin and prints payload."""
    script = tmp_path / "claude"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        f"log = {str(tmp_path / 'call.json')!r}\n"
        "json.dump({'argv': sys.argv[1:], 'cwd': os.getcwd(), 'stdin': sys.stdin.read(),\n"
        "           'api_key': os.environ.get('ANTHROPIC_API_KEY')}, open(log, 'w'))\n"
        f"print({json.dumps(json.dumps(payload))})\n"
        f"sys.exit({exit_code})\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def test_runs_headless_without_tools_and_parses(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    binary = _fake_claude(tmp_path, {"type": "result", "is_error": False, "stop_reason": "end_turn",
                                     "result": RESPONSE})
    summary = ClaudeCodeSummarizer(model="sonnet", binary=binary).summarize("hello transcript", user_notes="my note")

    call = json.loads((tmp_path / "call.json").read_text())
    argv = call["argv"]
    assert argv[0] == "-p"
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert argv[argv.index("--tools") + 1] == ""
    assert "--no-session-persistence" in argv and "--strict-mcp-config" in argv
    assert call["api_key"] is None, "subscription auth: API key must be stripped"
    assert "hello transcript" in call["stdin"] and "my note" in call["stdin"]
    assert "omascribe-claude-" in call["cwd"]

    assert summary.title == "Q3 'Inventory' Sync"
    assert summary.action_items == ["Priya to fix the inventory job by Friday"]
    assert summary.participants == ["Priya", "Sam"]
    assert summary.discussion.startswith("#### Inventory")
    assert "\n\nThe nightly job" in summary.discussion  # paragraph breaks kept


def test_error_result_raises(tmp_path):
    binary = _fake_claude(tmp_path, {"is_error": True, "result": "Not logged in"}, exit_code=1)
    with pytest.raises(RuntimeError, match="Not logged in"):
        ClaudeCodeSummarizer(binary=binary).summarize("x")


def test_missing_binary_and_bad_model(monkeypatch):
    monkeypatch.setattr(ClaudeCodeSummarizer, "find_binary", classmethod(lambda cls: None))
    with pytest.raises(ValueError, match="not found"):
        ClaudeCodeSummarizer()
    with pytest.raises(ValueError, match="Invalid model"):
        ClaudeCodeSummarizer(model="mini", binary="/bin/true")


def test_wired_into_config_and_note_maker(tmp_path, monkeypatch):
    assert "claude-code" in CLOUD_PROVIDERS
    assert validate_config(AppConfig(ai_provider="claude-code", ai_model="opus")) == (True, None)
    assert validate_config(AppConfig(ai_provider="claude-code", ai_model="mini"))[0] is False

    monkeypatch.setenv("OMASCRIBE_CLAUDE_BIN", _fake_claude(tmp_path, {"is_error": False, "result": RESPONSE}))
    maker = NoteMaker(output_dir=str(tmp_path / "notes"), transcripts_dir=str(tmp_path / "tr"),
                      ai_provider="claude-code", ai_model="sonnet")
    note_path, _, error = maker.create_note("hello there", "[00:00] hello there", 60.0)
    assert error is None
    note = open(note_path).read()
    assert 'title: "Q3 \'Inventory\' Sync"' in note
    assert "### Discussion" in note and "#### Inventory" in note
    assert "q3-inventory-sync" in note_path

    # A title typed during recording wins over the suggested one.
    note_path, _, _ = maker.create_note("hi", "hi", 5.0, title="Trader Meeting")
    assert 'title: "Trader Meeting"' in open(note_path).read()

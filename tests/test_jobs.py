"""Tests for the transcription job registry."""

import json
import os
import wave

from omascribe import desktop, jobs


def _wav(path, seconds=2, rate=8000):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\0\0" * rate * seconds)
    return path


def test_job_lifecycle(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    audio = _wav(tmp_path / "a.wav")

    job_id = jobs.create_job(str(audio), "Standup", "my notes")
    [job] = jobs.list_jobs()
    assert job["id"] == job_id
    assert job["state"] == jobs.RUNNING
    assert job["title"] == "Standup"
    assert job["audio_seconds"] == 2
    path = jobs.jobs_dir() / f"{job_id}.json"
    assert path.stat().st_mode & 0o777 == 0o600
    assert jobs.jobs_dir().stat().st_mode & 0o777 == 0o700

    jobs.update_stage(job_id, "Transcribing audio")
    assert jobs.list_jobs()[0]["stage"] == "Transcribing audio"

    jobs.remove_job(job_id)
    assert jobs.list_jobs() == []


def test_dead_owner_is_interrupted(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    job_id = jobs.create_job(str(tmp_path / "missing.wav"))
    path = jobs.jobs_dir() / f"{job_id}.json"
    payload = json.loads(path.read_text())
    payload["start_time"] = "some-other-process"
    path.write_text(json.dumps(payload))

    [job] = jobs.list_jobs()
    assert job["state"] == jobs.INTERRUPTED
    assert job["audio_seconds"] is None


def test_failed_job(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    job_id = jobs.create_job(str(tmp_path / "a.wav"))
    jobs.fail_job(job_id, "boom")
    [job] = jobs.list_jobs()
    assert job["state"] == jobs.FAILED
    assert job["error"] == "boom"


def test_retry_replaces_entry_for_same_audio(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    first = jobs.create_job(str(tmp_path / "a.wav"))
    jobs.fail_job(first, "boom")
    second = jobs.create_job(str(tmp_path / "a.wav"))
    assert [j["id"] for j in jobs.list_jobs()] == [second]


def test_malformed_job_files_are_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    jobs.jobs_dir().mkdir(parents=True)
    (jobs.jobs_dir() / "bad.json").write_text("not json")
    (jobs.jobs_dir() / "list.json").write_text("[]")
    assert jobs.list_jobs() == []


def test_bar_and_panel_report_running_transcription(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    jobs.create_job(str(tmp_path / "a.wav"), "Planning")

    desktop.bar_status()
    bar = json.loads(capsys.readouterr().out)
    assert bar["class"] == "processing"
    assert "Planning" in bar["tooltip"]

    monkeypatch.setenv("HOME", str(tmp_path))
    desktop.panel_data()
    panel = json.loads(capsys.readouterr().out)
    assert panel["status"]["state"] == "processing"
    assert panel["transcriptions"][0]["title"] == "Planning"


def test_format_seconds():
    assert jobs.format_seconds(None) == "<1m"
    assert jobs.format_seconds(59) == "<1m"
    assert jobs.format_seconds(125) == "2m"
    assert jobs.format_seconds(3 * 3600 + 5 * 60) == "3h 05m"

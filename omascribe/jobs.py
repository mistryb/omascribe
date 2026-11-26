"""Registry of recordings being transcribed.

Every transcription the app starts writes a small JSON file under
``$XDG_STATE_HOME/omascribe/jobs/``. It's removed once the note is saved, so
whatever is left is either still running, failed, or was interrupted (the
app that owned it was closed or crashed mid-transcription).

Keeping this on disk, rather than only in app memory, means a relaunched app
— or the desktop panel — can still see transcriptions another instance is
running, and can tell the user about ones that never finished instead of the
meeting silently disappearing.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
import wave
from pathlib import Path
from typing import Optional

from .desktop import _process_identity

RUNNING = "running"
FAILED = "failed"
INTERRUPTED = "interrupted"


def jobs_dir() -> Path:
    """Return the private directory holding job files."""
    state_home = os.environ.get("XDG_STATE_HOME")
    state_dir = Path(state_home) if state_home else Path.home() / ".local" / "state"
    return state_dir / "omascribe" / "jobs"


def _job_path(job_id: str) -> Path:
    return jobs_dir() / f"{job_id}.json"


def _write(job: dict) -> None:
    directory = jobs_dir()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    fd, temporary = tempfile.mkstemp(dir=directory, prefix=".job-")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(job, stream, ensure_ascii=False)
            stream.write("\n")
        os.replace(temporary, _job_path(job["id"]))
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _read(job_id: str) -> Optional[dict]:
    try:
        return json.loads(_job_path(job_id).read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return None


def audio_duration(audio_path: str) -> Optional[float]:
    """Length of a WAV file in seconds, or None when it can't be read cheaply."""
    try:
        with wave.open(str(audio_path), "rb") as wav:
            rate = wav.getframerate()
            return wav.getnframes() / rate if rate else None
    except (wave.Error, OSError, EOFError):
        return None


def create_job(audio_path: str, title: Optional[str] = None, user_notes: str = "") -> str:
    """Register a transcription owned by this process and return its id.

    Any earlier entry for the same audio file (e.g. an interrupted attempt
    being retried) is replaced, so a recording never shows up twice.
    """
    resolved = str(Path(audio_path).expanduser().absolute())
    for job in list_jobs():
        if job.get("audio_path") == resolved:
            remove_job(job["id"])

    boot_id, start_time = _process_identity(os.getpid())
    job = {
        "version": 1,
        "id": uuid.uuid4().hex,
        "audio_path": resolved,
        "title": title or "",
        "user_notes": user_notes or "",
        "audio_seconds": audio_duration(resolved),
        "status": RUNNING,
        "stage": "Preparing transcription",
        "error": "",
        "started_at": time.time(),
        "pid": os.getpid(),
        "boot_id": boot_id,
        "start_time": start_time,
    }
    _write(job)
    return job["id"]


def update_stage(job_id: str, stage: str) -> None:
    job = _read(job_id)
    if job is not None:
        job["stage"] = stage
        _write(job)


def fail_job(job_id: str, error: str) -> None:
    job = _read(job_id)
    if job is not None:
        job["status"] = FAILED
        job["error"] = error
        _write(job)


def remove_job(job_id: str) -> None:
    try:
        _job_path(job_id).unlink()
    except FileNotFoundError:
        pass


def _owner_alive(job: dict) -> bool:
    try:
        boot_id, start_time = _process_identity(int(job["pid"]))
    except (KeyError, TypeError, ValueError):
        return False
    return bool(boot_id and start_time) and job.get("boot_id") == boot_id and job.get("start_time") == start_time


def list_jobs() -> list[dict]:
    """Return all unfinished jobs, newest first, each with a resolved ``state``.

    ``state`` is ``running`` while the owning process is alive, ``failed``
    when processing raised, and ``interrupted`` when the owner went away
    without finishing.
    """
    directory = jobs_dir()
    if not directory.is_dir():
        return []
    jobs = []
    for path in directory.glob("*.json"):
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(job, dict) or "id" not in job:
            continue
        if job.get("status") == FAILED:
            job["state"] = FAILED
        elif _owner_alive(job):
            job["state"] = RUNNING
        else:
            job["state"] = INTERRUPTED
        jobs.append(job)
    jobs.sort(key=lambda j: j.get("started_at") or 0, reverse=True)
    return jobs


def display_title(job: dict) -> str:
    return job.get("title") or Path(job.get("audio_path", "recording")).stem


def format_seconds(seconds: Optional[float]) -> str:
    if not seconds or seconds < 60:
        return "<1m"
    minutes, _ = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m"

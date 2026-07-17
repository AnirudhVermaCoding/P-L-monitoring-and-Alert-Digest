"""Process-wide store for background pipeline jobs.

Lives in its own module (not app.py) on purpose: Streamlit re-executes the main script
top-to-bottom on every rerun, which would reset a dict defined there. Imported modules are
cached in sys.modules, so this dict persists across reruns — letting a worker thread and the
UI's polling fragment share job state reliably, and letting the user leave and come back.
"""
from __future__ import annotations

import threading
import uuid
from typing import Callable

JOBS: dict[str, dict] = {}
_LOCK = threading.Lock()


def create_job() -> str:
    job_id = uuid.uuid4().hex
    with _LOCK:
        JOBS[job_id] = {"status": "running", "progress": "Starting…",
                        "result": None, "error": None}
    return job_id


def get_job(job_id: str) -> dict | None:
    return JOBS.get(job_id)


def start(worker: Callable[[str], None]) -> str:
    """Create a job and run `worker(job_id)` on a daemon thread. Returns the job id."""
    job_id = create_job()
    threading.Thread(target=worker, args=(job_id,), daemon=True).start()
    return job_id

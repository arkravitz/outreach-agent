"""Supervise one UI-owned consumer; never signal arbitrary or external processes."""

from datetime import datetime
from pathlib import Path
import os
import subprocess
import sys
import threading
from .models import now


class WorkerControl:
    def __init__(self, state, store):
        self.state, self.store = Path(state).resolve(), store
        self.process = None
        self.lock = threading.Lock()
        self.log = None

    def status(self):
        owned = bool(self.process and self.process.poll() is None)
        row = self.store.one("select value from sync_state where key='worker_heartbeat'")
        age = (now() - datetime.fromisoformat(row["value"])).total_seconds() if row else None
        external = self.process is None and age is not None and 0 <= age < 120
        return {
            "running": owned or external,
            "owned": owned,
            "external": external,
            "heartbeat_at": row["value"] if row else None,
            "last_exit_code": self.process.poll() if self.process else None,
        }

    def start(self):
        with self.lock:
            if self.status()["running"]:
                return self.status()
            executable = Path(sys.executable).parent / "huey_consumer"
            if not executable.exists():
                raise ValueError(
                    "Worker executable missing; reinstall this package in its virtual environment"
                )
            if self.log:
                self.log.close()
            self.log = open(self.state / "worker.log", "ab", buffering=0)
            os.chmod(self.state / "worker.log", 0o600)
            self.process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "pipeline.worker_host",
                    "pipeline.worker.huey",
                    "-w",
                    "1",
                    "-k",
                    "thread",
                    "-g",
                    "TERM",
                    "-t",
                    "60",
                ],
                env={**os.environ, "PIPELINE_STATE": str(self.state)},
                stdout=self.log,
                stderr=subprocess.STDOUT,
                close_fds=True,
                start_new_session=True,
            )
            return self.status()

    def stop(self):
        with self.lock:
            if self.process and self.process.poll() is None:
                self.process.terminate()
                return {**self.status(), "stopping": True}
            if self.status()["external"]:
                raise ValueError(
                    "This worker is managed outside the app. Stop its terminal or system service."
                )
            return self.status()

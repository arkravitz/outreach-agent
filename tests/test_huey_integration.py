"""Real persistent Huey consumer subprocess, with no external provider calls."""

import json
import os
from pathlib import Path
import subprocess
import sys
import time
from pipeline.db import Store


def test_real_huey_consumer_executes_durable_job(tmp_path):
    state = tmp_path / "host"
    store = Store(state / "pipeline.db")
    store.enqueue("host-digest", "digest", {})
    env = {**os.environ, "PIPELINE_STATE": str(state)}
    subprocess.run(
        [sys.executable, "-c", "from pipeline.worker import execute_job; execute_job('host-digest')"],
        env=env,
        check=True,
        capture_output=True,
    )
    consumer = Path(sys.executable).parent / "huey_consumer"
    process = subprocess.Popen(
        [str(consumer), "pipeline.worker.huey", "-w", "1", "-k", "thread", "-n"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if store.one("select status from jobs where id='host-digest'")["status"] == "DONE":
                break
            if process.poll() is not None:
                raise AssertionError(process.stdout.read().decode())
            time.sleep(0.05)
        assert store.one("select status from jobs where id='host-digest'")["status"] == "DONE"
        report = store.one("select value from sync_state where key='daily_digest'")
        assert json.loads(report["value"])["new_prospects_contacted"] == 0
    finally:
        process.terminate()
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()


def test_supervised_worker_wakes_outbox_without_waiting_for_next_minute(tmp_path):
    from pipeline.worker_control import WorkerControl

    state = tmp_path / "supervised"
    store = Store(state / "pipeline.db")
    control = WorkerControl(state, store)
    try:
        assert control.start()["running"]
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            heartbeat = store.one("select value from sync_state where key='worker_heartbeat'")
            if heartbeat:
                break
            if control.process.poll() is not None:
                raise AssertionError((state / "worker.log").read_text())
            time.sleep(0.05)
        assert store.one("select value from sync_state where key='worker_heartbeat'")
    finally:
        control.stop()
        if control.process:
            control.process.wait(timeout=5)

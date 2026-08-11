# Persistent host

Use one Linux host, one worker consumer and local persistent disk. SQLite WAL files must stay on the same local volume. No NFS or multi-host shared database. Python 3.12 is the tested runtime. There is no deployed host or service created by this repository.

1. Copy the repo to /opt/pipeline, create a non-login `pipeline` OS user, create its venv and `uv sync --frozen --extra dev` (omit dev for production).
2. Create /var/lib/pipeline owned by that user, mode 700. Put Google OAuth token there, mode 600. Put credentials/models/environment in /etc/pipeline.env, readable only by the service user/root. Use absolute paths. Install the actual no-ai-slop skill plus eval.md in a readable path and set NO_AI_SLOP_DIR.
3. Configure campaigns using the service environment and the same PIPELINE_STATE. Initialize review mode; configure sender details and check `pipeline doctor`.
4. Review deploy/pipeline.service paths, copy it to /etc/systemd/system/pipeline.service, then `sudo systemctl daemon-reload` and `sudo systemctl enable --now pipeline`.
5. Inspect `journalctl -u pipeline` and `pipeline failures`. Use `pipeline digest` for a current report. Structured audit events are in SQLite; provider payloads and credentials are not logged.

The worker polls Gmail each minute, generates one daily digest record and makes one bounded discovery pass per configured campaign per UTC day when automatic discovery is enabled in Setup & costs. Rate/pause/approval blocks wait five minutes before another attempt. Provider failures back off 30, 60, 120, 240 seconds; the fifth failure dead-letters the job. Expired fifteen-minute job leases recover automatically. Ambiguous sends remain UNKNOWN and require reconciliation. Huey tasks have a 600-second execution timeout; requests have shorter timeouts. Use a single consumer to keep token refreshes and polling serialized.

`pipeline pause` blocks email sends and Calendar invitations. Inbox sync continues so replies/opt-outs can be received. To stop every activity: `sudo systemctl stop pipeline`. Kill/interrupting a send can leave an UNKNOWN/SENDING reservation; reconcile it before taking further action.

Back up with SQLite's online backup API (`sqlite3.Connection.backup`) for pipeline.db and queue.db, or stop the service and copy the whole state directory including WAL files. Protect backups because they contain correspondence and suppression records. Restore both databases together. Keep suppression data when archiving campaigns or pruning research. No retention purge is enabled by default.

For local development on macOS, run `huey_consumer pipeline.worker.huey -w 1 -k thread` in a terminal. Use launchd or a Linux service for unattended operation; a sleeping laptop is not a reliable host.

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

from .models import RunMode, RunState


def utcnow_iso() -> str:
    return datetime.now(UTC).isoformat()


class Database:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self) -> None:
        with self.transaction() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    repo TEXT NOT NULL,
                    prompt_version TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    budget_json TEXT NOT NULL,
                    workspace_dir TEXT NOT NULL,
                    pr_number INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS run_states (
                    run_id TEXT PRIMARY KEY REFERENCES runs(run_id) ON DELETE CASCADE,
                    current_state TEXT NOT NULL,
                    last_error TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                    event_type TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(run_id, idempotency_key)
                );

                CREATE TABLE IF NOT EXISTS step_attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                    step TEXT NOT NULL,
                    attempt_no INTEGER NOT NULL,
                    exit_code INTEGER NOT NULL,
                    stdout_log TEXT NOT NULL,
                    stderr_log TEXT NOT NULL,
                    duration_ms INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS artifacts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                    artifact_type TEXT NOT NULL,
                    uri TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS webhook_deliveries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL,
                    delivery_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    UNIQUE(source, delivery_id)
                );

                CREATE INDEX IF NOT EXISTS idx_events_run_created
                ON events(run_id, created_at);

                CREATE INDEX IF NOT EXISTS idx_attempts_run_step
                ON step_attempts(run_id, step, attempt_no);

                CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_received
                ON webhook_deliveries(source, received_at);
                """
            )

    def insert_run(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: str,
        owner: str,
        repo: str,
        prompt_version: str,
        mode: RunMode,
        budget: dict[str, Any],
        workspace_dir: str,
    ) -> None:
        now = utcnow_iso()
        conn.execute(
            """
            INSERT INTO runs (
                run_id, owner, repo, prompt_version, mode, budget_json,
                workspace_dir, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                owner,
                repo,
                prompt_version,
                mode.value,
                json.dumps(budget, sort_keys=True),
                workspace_dir,
                now,
                now,
            ),
        )
        conn.execute(
            """
            INSERT INTO run_states (run_id, current_state, last_error, updated_at)
            VALUES (?, ?, NULL, ?)
            """,
            (run_id, RunState.QUEUED.value, now),
        )

    def insert_event(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: str,
        event_type: str,
        idempotency_key: str,
        payload: dict[str, Any],
    ) -> bool:
        try:
            conn.execute(
                """
                INSERT INTO events (run_id, event_type, idempotency_key, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    event_type,
                    idempotency_key,
                    json.dumps(payload, sort_keys=True),
                    utcnow_iso(),
                ),
            )
        except sqlite3.IntegrityError:
            return False
        return True

    def get_run(self, conn: sqlite3.Connection, run_id: str) -> dict[str, Any] | None:
        row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["budget"] = json.loads(result.pop("budget_json"))
        return result

    def list_runs(self, conn: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
        rows = conn.execute(
            """
            SELECT
                r.run_id,
                r.owner,
                r.repo,
                r.mode,
                r.prompt_version,
                r.pr_number,
                s.current_state,
                s.last_error,
                s.updated_at
            FROM runs r
            JOIN run_states s ON s.run_id = r.run_id
            ORDER BY s.updated_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_state(self, conn: sqlite3.Connection, run_id: str) -> RunState:
        row = conn.execute(
            "SELECT current_state FROM run_states WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"Run not found: {run_id}")
        return RunState(row["current_state"])

    def set_state(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: str,
        target: RunState,
        last_error: str | None = None,
    ) -> None:
        now = utcnow_iso()
        conn.execute(
            """
            UPDATE run_states
            SET current_state = ?, last_error = ?, updated_at = ?
            WHERE run_id = ?
            """,
            (target.value, last_error, now, run_id),
        )
        conn.execute("UPDATE runs SET updated_at = ? WHERE run_id = ?", (now, run_id))

    def set_pr_number(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: str,
        pr_number: int,
    ) -> None:
        conn.execute(
            "UPDATE runs SET pr_number = ?, updated_at = ? WHERE run_id = ?",
            (pr_number, utcnow_iso(), run_id),
        )

    def insert_step_attempt(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: str,
        step: str,
        exit_code: int,
        stdout_log: str,
        stderr_log: str,
        duration_ms: int,
    ) -> None:
        row = conn.execute(
            """
            SELECT COALESCE(MAX(attempt_no), 0) AS current_max
            FROM step_attempts
            WHERE run_id = ? AND step = ?
            """,
            (run_id, step),
        ).fetchone()
        attempt_no = int(row["current_max"]) + 1
        conn.execute(
            """
            INSERT INTO step_attempts (
                run_id, step, attempt_no, exit_code, stdout_log, stderr_log, duration_ms, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                step,
                attempt_no,
                exit_code,
                stdout_log,
                stderr_log,
                duration_ms,
                utcnow_iso(),
            ),
        )

    def insert_artifact(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: str,
        artifact_type: str,
        uri: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        conn.execute(
            """
            INSERT INTO artifacts (run_id, artifact_type, uri, metadata_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                run_id,
                artifact_type,
                uri,
                json.dumps(metadata or {}, sort_keys=True),
                utcnow_iso(),
            ),
        )

    def get_run_snapshot(self, run_id: str) -> dict[str, Any]:
        with self.transaction() as conn:
            run = self.get_run(conn, run_id)
            if run is None:
                raise KeyError(f"Run not found: {run_id}")
            state = self.get_state(conn, run_id)
            return {
                "run": run,
                "state": state.value,
            }

    def get_run_snapshot_by_pr_number(self, pr_number: int) -> dict[str, Any] | None:
        with self.transaction() as conn:
            row = conn.execute(
                """
                SELECT r.*
                FROM runs r
                WHERE r.pr_number = ?
                ORDER BY r.updated_at DESC
                LIMIT 1
                """,
                (pr_number,),
            ).fetchone()
            if row is None:
                return None
            run = dict(row)
            run["budget"] = json.loads(run.pop("budget_json"))
            state = self.get_state(conn, run["run_id"])
            return {
                "run": run,
                "state": state.value,
            }

    def get_run_snapshot_by_repo_and_pr_number(
        self,
        *,
        owner: str,
        repo: str,
        pr_number: int,
    ) -> dict[str, Any] | None:
        with self.transaction() as conn:
            row = conn.execute(
                """
                SELECT r.*
                FROM runs r
                WHERE r.owner = ? AND r.repo = ? AND r.pr_number = ?
                ORDER BY r.updated_at DESC
                LIMIT 1
                """,
                (owner, repo, pr_number),
            ).fetchone()
            if row is None:
                return None
            run = dict(row)
            run["budget"] = json.loads(run.pop("budget_json"))
            state = self.get_state(conn, run["run_id"])
            return {
                "run": run,
                "state": state.value,
            }

    def list_artifacts(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: str,
        artifact_type: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        if artifact_type is None:
            rows = conn.execute(
                """
                SELECT id, run_id, artifact_type, uri, metadata_json, created_at
                FROM artifacts
                WHERE run_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (run_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT id, run_id, artifact_type, uri, metadata_json, created_at
                FROM artifacts
                WHERE run_id = ? AND artifact_type = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (run_id, artifact_type, limit),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["metadata"] = json.loads(item.pop("metadata_json"))
            result.append(item)
        return result

    def reserve_webhook_delivery(
        self,
        conn: sqlite3.Connection,
        *,
        source: str,
        delivery_id: str,
        event_type: str,
        payload_sha256: str,
    ) -> bool:
        try:
            conn.execute(
                """
                INSERT INTO webhook_deliveries (
                    source, delivery_id, event_type, payload_sha256, received_at
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    source,
                    delivery_id,
                    event_type,
                    payload_sha256,
                    utcnow_iso(),
                ),
            )
        except sqlite3.IntegrityError:
            return False
        return True

    def cleanup_webhook_deliveries(
        self,
        conn: sqlite3.Connection,
        *,
        source: str,
        keep_after_iso: str,
    ) -> int:
        cursor = conn.execute(
            """
            DELETE FROM webhook_deliveries
            WHERE source = ? AND received_at < ?
            """,
            (source, keep_after_iso),
        )
        return int(cursor.rowcount)

    def delete_webhook_delivery(
        self,
        conn: sqlite3.Connection,
        *,
        source: str,
        delivery_id: str,
    ) -> int:
        cursor = conn.execute(
            """
            DELETE FROM webhook_deliveries
            WHERE source = ? AND delivery_id = ?
            """,
            (source, delivery_id),
        )
        return int(cursor.rowcount)

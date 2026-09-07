"""SQLite checkpoint, command idempotency, event and config snapshot storage."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Iterable

from .config import ResolvedRunConfig
from .models import DiagnosisState
from .protocol.commands import AgentCommand
from .protocol.events import AgentEvent


class RunNotFoundError(ValueError):
    code = "run_not_found"


class RevisionConflictError(ValueError):
    code = "revision_conflict"


class InvalidRunStatusError(ValueError):
    code = "invalid_run_status"


class PendingRequestMismatchError(ValueError):
    code = "pending_request_mismatch"


class ValidationFailedError(ValueError):
    code = "validation_failed"


class LeaseConflictError(RuntimeError):
    code = "run_busy"


class SQLiteCheckpointStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS diagnosis_runs (
                run_id TEXT PRIMARY KEY,
                revision INTEGER NOT NULL,
                state_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS diagnosis_commands (
                command_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                event_ids_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS diagnosis_events (
                event_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                revision INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                event_json TEXT NOT NULL,
                UNIQUE(run_id, sequence)
            );
            CREATE TABLE IF NOT EXISTS run_config_snapshots (
                snapshot_id TEXT PRIMARY KEY,
                profile TEXT NOT NULL,
                config_version TEXT NOT NULL,
                prompt_config_version TEXT NOT NULL,
                config_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS run_leases (
                run_id TEXT PRIMARY KEY,
                owner TEXT NOT NULL,
                expires_at REAL NOT NULL
            );
            """
        )
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def save_config_snapshot(self, config: ResolvedRunConfig) -> None:
        self.db.execute(
            """INSERT OR IGNORE INTO run_config_snapshots
            (snapshot_id, profile, config_version, prompt_config_version, config_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)""",
            (
                config.snapshot_id,
                config.profile,
                config.config_version,
                config.prompt_config_version,
                config.as_json(),
                config.resolved_at.isoformat(),
            ),
        )
        self.db.commit()

    def get_config_snapshot(self, snapshot_id: str) -> ResolvedRunConfig:
        row = self.db.execute(
            "SELECT config_json FROM run_config_snapshots WHERE snapshot_id = ?",
            (snapshot_id,),
        ).fetchone()
        if row is None:
            raise RunNotFoundError(f"config snapshot not found: {snapshot_id}")
        return ResolvedRunConfig.model_validate_json(row["config_json"])

    def get_state(self, run_id: str) -> DiagnosisState:
        row = self.db.execute(
            "SELECT state_json FROM diagnosis_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise RunNotFoundError(f"run not found: {run_id}")
        return DiagnosisState.model_validate_json(row["state_json"])

    def get_command(self, command_id: str) -> list[AgentEvent] | None:
        row = self.db.execute(
            "SELECT event_ids_json FROM diagnosis_commands WHERE command_id = ?",
            (command_id,),
        ).fetchone()
        if row is None:
            return None
        events: list[AgentEvent] = []
        for event_id in json.loads(row["event_ids_json"]):
            event = self.db.execute(
                "SELECT event_json FROM diagnosis_events WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            if event:
                events.append(event_from_json(event["event_json"]))
        return events

    def create_run(
        self, state: DiagnosisState, events: Iterable[AgentEvent], command: AgentCommand
    ) -> None:
        event_list = list(events)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            if self.db.execute(
                "SELECT 1 FROM diagnosis_runs WHERE run_id = ?", (state.run_id,)
            ).fetchone():
                raise RevisionConflictError("run already exists")
            self.db.execute(
                "INSERT INTO diagnosis_runs VALUES (?, ?, ?, ?)",
                (
                    state.run_id,
                    state.revision,
                    state.model_dump_json(),
                    state.updated_at.isoformat(),
                ),
            )
            self._insert_events(event_list)
            self._insert_command(command, event_list)
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def commit(
        self,
        state: DiagnosisState,
        expected_revision: int,
        events: Iterable[AgentEvent],
        command: AgentCommand | None = None,
    ) -> None:
        event_list = list(events)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute(
                "SELECT revision FROM diagnosis_runs WHERE run_id = ?", (state.run_id,)
            ).fetchone()
            if row is None:
                raise RunNotFoundError(f"run not found: {state.run_id}")
            if row["revision"] != expected_revision:
                raise RevisionConflictError("run revision changed")
            self.db.execute(
                "UPDATE diagnosis_runs SET revision = ?, state_json = ?, updated_at = ? WHERE run_id = ?",
                (
                    state.revision,
                    state.model_dump_json(),
                    state.updated_at.isoformat(),
                    state.run_id,
                ),
            )
            self._insert_events(event_list)
            if command is not None:
                self._insert_command(command, event_list)
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def events_after(self, run_id: str, sequence: int = 0) -> list[AgentEvent]:
        rows = self.db.execute(
            "SELECT event_json FROM diagnosis_events WHERE run_id = ? AND sequence > ? ORDER BY sequence",
            (run_id, sequence),
        ).fetchall()
        return [event_from_json(row["event_json"]) for row in rows]

    def list_states(
        self,
        *,
        lifecycle_status: str | None = None,
        outcome: str | None = None,
        current_node: str | None = None,
        profile: str | None = None,
        query: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[list[DiagnosisState], int]:
        """Return a bounded, read-only view of persisted runs for admin surfaces.

        Filtering is deliberately performed after deserialisation. ``DiagnosisState``
        remains the source of truth for lifecycle fields, keeping the admin adapter
        independent from the rest of the checkpoint schema.
        """

        rows = self.db.execute(
            "SELECT state_json FROM diagnosis_runs ORDER BY updated_at DESC, run_id DESC"
        ).fetchall()
        normalized_query = query.strip().lower() if query else None
        matches: list[DiagnosisState] = []
        for row in rows:
            state = DiagnosisState.model_validate_json(row["state_json"])
            if lifecycle_status and state.lifecycle_status.value != lifecycle_status:
                continue
            if outcome and (state.outcome is None or state.outcome.value != outcome):
                continue
            if current_node and (
                state.current_node is None or state.current_node.value != current_node
            ):
                continue
            if profile and state.config_profile != profile:
                continue
            if normalized_query:
                haystack = f"{state.run_id} {state.user_question}".lower()
                if normalized_query not in haystack:
                    continue
            matches.append(state)
        start = max(0, offset)
        end = start + max(1, min(limit, 100))
        return matches[start:end], len(matches)

    def list_sessions(
        self,
        *,
        run_id: str | None = None,
        node: str | None = None,
        status: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[list[dict[str, object]], int]:
        """Return SDK-managed session metadata without exposing model messages."""

        try:
            rows = self.db.execute(
                "SELECT session_id, created_at, updated_at FROM agent_sessions "
                "ORDER BY updated_at DESC, session_id DESC"
            ).fetchall()
        except sqlite3.OperationalError:
            # A fresh installation has no SDK session tables until the first node
            # runs. The admin API should still be healthy in that state.
            return [], 0

        message_counts: dict[str, int] = {}
        try:
            count_rows = self.db.execute(
                "SELECT session_id, COUNT(*) AS count FROM agent_messages GROUP BY session_id"
            ).fetchall()
            message_counts = {
                str(row["session_id"]): int(row["count"]) for row in count_rows
            }
        except sqlite3.OperationalError:
            pass

        states: dict[str, DiagnosisState] = {}
        state_rows = self.db.execute("SELECT state_json FROM diagnosis_runs").fetchall()
        for row in state_rows:
            state = DiagnosisState.model_validate_json(row["state_json"])
            states[state.run_id] = state

        records: list[dict[str, object]] = []
        for row in rows:
            session_id = str(row["session_id"])
            if ":" in session_id:
                session_run_id, session_node = session_id.rsplit(":", 1)
            else:
                session_run_id, session_node = session_id, None
            if run_id and session_run_id != run_id:
                continue
            if node and session_node != node:
                continue
            state = states.get(session_run_id)
            if state is None:
                session_status = "unknown"
            elif state.lifecycle_status.value == "waiting_user":
                session_status = "waiting"
            elif state.lifecycle_status.value in {"completed", "failed"}:
                session_status = "completed"
            else:
                session_status = "active"
            if status and session_status != status:
                continue
            records.append(
                {
                    "session_id": session_id,
                    "run_id": session_run_id,
                    "node": session_node,
                    "status": session_status,
                    "created_at": str(row["created_at"]),
                    "updated_at": str(row["updated_at"]),
                    "message_count": message_counts.get(session_id, 0),
                    "config_snapshot_id": (
                        state.config_snapshot_id if state is not None else None
                    ),
                }
            )
        start = max(0, offset)
        end = start + max(1, min(limit, 100))
        return records[start:end], len(records)

    def acquire_lease(self, run_id: str, owner: str, seconds: int = 60) -> None:
        now = time.time()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute(
                "SELECT owner, expires_at FROM run_leases WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row and row["expires_at"] > now and row["owner"] != owner:
                raise LeaseConflictError("run is already being processed")
            self.db.execute(
                "INSERT OR REPLACE INTO run_leases VALUES (?, ?, ?)",
                (run_id, owner, now + seconds),
            )
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def release_lease(self, run_id: str, owner: str) -> None:
        self.db.execute(
            "DELETE FROM run_leases WHERE run_id = ? AND owner = ?", (run_id, owner)
        )
        self.db.commit()

    def _insert_events(self, events: list[AgentEvent]) -> None:
        for event in events:
            self.db.execute(
                "INSERT INTO diagnosis_events VALUES (?, ?, ?, ?, ?, ?)",
                (
                    event.event_id,
                    event.run_id,
                    event.sequence,
                    event.revision,
                    event.event_type,
                    event.model_dump_json(),
                ),
            )

    def _insert_command(self, command: AgentCommand, events: list[AgentEvent]) -> None:
        self.db.execute(
            "INSERT INTO diagnosis_commands VALUES (?, ?, ?, datetime('now'))",
            (
                command.command_id,
                command.run_id,
                json.dumps([e.event_id for e in events]),
            ),
        )


def event_from_json(value: str) -> AgentEvent:
    from .protocol.events import (
        InputRequired,
        NodeCompleted,
        NodeStarted,
        RunCanceled,
        RunCompleted,
        RunFailed,
        RunStarted,
        RunWaiting,
    )

    classes = {
        "run_started": RunStarted,
        "node_started": NodeStarted,
        "node_completed": NodeCompleted,
        "input_required": InputRequired,
        "run_waiting": RunWaiting,
        "run_completed": RunCompleted,
        "run_failed": RunFailed,
        "run_canceled": RunCanceled,
    }
    data = json.loads(value)
    return classes[data["event_type"]].model_validate(data)

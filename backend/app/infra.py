"""SQLite checkpoint, audit ledger, command idempotency and lease storage."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

from .config import ResolvedRunConfig
from .environment import ResolvedEnvironmentSnapshot
from .models import (
    DiagnosisState,
    EvidenceRecord,
    NodeExecutionPlan,
    replace_state,
)
from .protocol.commands import AgentCommand
from .protocol.events import AgentEvent
from .security import sanitize_data


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


class FencingTokenError(RuntimeError):
    code = "lease_fenced"


class SQLiteCheckpointStore:
    """Small SQLite repository with explicit, repeatable schema migrations."""

    SCHEMA_VERSION = 3

    def __init__(self, path: Path) -> None:
        self.path = path
        self.last_list_degraded_count = 0
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def _migrate(self) -> None:
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS buglens_schema (
                singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                version INTEGER NOT NULL
            );
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
                command_json TEXT,
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
            CREATE TABLE IF NOT EXISTS run_environment_snapshots (
                snapshot_id TEXT PRIMARY KEY,
                environment_id TEXT NOT NULL,
                config_revision TEXT NOT NULL,
                snapshot_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS run_leases (
                run_id TEXT PRIMARY KEY,
                owner TEXT NOT NULL,
                fencing_token INTEGER NOT NULL DEFAULT 0,
                expires_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS diagnosis_state_history (
                run_id TEXT NOT NULL,
                revision INTEGER NOT NULL,
                state_json TEXT NOT NULL,
                cause_event_id TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY (run_id, revision)
            );
            CREATE TABLE IF NOT EXISTS diagnosis_node_executions (
                execution_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                node TEXT NOT NULL,
                session_id TEXT NOT NULL,
                investigation_attempt INTEGER NOT NULL,
                clarification_round INTEGER NOT NULL,
                retry_cycle INTEGER NOT NULL,
                retry_index INTEGER NOT NULL,
                status TEXT NOT NULL,
                input_context_json TEXT,
                input_hash TEXT,
                output_json TEXT,
                error_code TEXT,
                error_message TEXT,
                retryable INTEGER NOT NULL DEFAULT 0,
                reasoning_summary_json TEXT,
                trace_id TEXT,
                config_snapshot_id TEXT NOT NULL,
                agent_definition_version TEXT NOT NULL,
                started_at TEXT NOT NULL,
                completed_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_node_executions_run_node
                ON diagnosis_node_executions(run_id, node, started_at);
            CREATE TABLE IF NOT EXISTS diagnosis_tool_executions (
                tool_execution_id TEXT PRIMARY KEY,
                node_execution_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                sdk_tool_call_id TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                tool_version TEXT NOT NULL,
                plugin_id TEXT,
                plugin_implementation_version TEXT,
                plugin_instance_id TEXT,
                environment_snapshot_id TEXT,
                source_id TEXT,
                operation TEXT,
                redacted_query TEXT,
                query_fingerprint TEXT,
                retry_index INTEGER NOT NULL,
                arguments_json TEXT,
                arguments_hash TEXT,
                status TEXT NOT NULL,
                error_code TEXT,
                error_message TEXT,
                retryable INTEGER NOT NULL DEFAULT 0,
                result_summary_json TEXT,
                evidence_ids_json TEXT NOT NULL DEFAULT '[]',
                truncated INTEGER NOT NULL DEFAULT 0,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                duration_ms INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_tool_executions_node
                ON diagnosis_tool_executions(node_execution_id, started_at);
            CREATE TABLE IF NOT EXISTS diagnosis_evidence (
                evidence_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                source_type TEXT NOT NULL,
                source_reference TEXT,
                content_hash TEXT NOT NULL,
                content TEXT,
                storage_reference TEXT,
                observed_at TEXT,
                collected_at TEXT NOT NULL,
                tool_execution_id TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_evidence_run
                ON diagnosis_evidence(run_id, collected_at);
            CREATE TABLE IF NOT EXISTS diagnosis_sdk_run_states (
                run_id TEXT NOT NULL,
                request_id TEXT NOT NULL,
                encrypted_state TEXT NOT NULL,
                sdk_version TEXT NOT NULL,
                agent_definition_version TEXT NOT NULL,
                created_at TEXT NOT NULL,
                resolved_at TEXT,
                decision TEXT,
                decision_reason TEXT,
                PRIMARY KEY(run_id, request_id)
            );
            CREATE TABLE IF NOT EXISTS diagnosis_run_control (
                run_id TEXT PRIMARY KEY,
                cancel_requested_at TEXT,
                cancel_command_id TEXT,
                reason TEXT,
                processed_at TEXT
            );
            """
        )
        self._ensure_column("diagnosis_commands", "command_json", "TEXT")
        self._ensure_column("run_leases", "fencing_token", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column(
            "diagnosis_node_executions", "reasoning_summary_json", "TEXT"
        )
        for column, definition in (
            ("plugin_id", "TEXT"),
            ("plugin_implementation_version", "TEXT"),
            ("plugin_instance_id", "TEXT"),
            ("environment_snapshot_id", "TEXT"),
            ("source_id", "TEXT"),
            ("operation", "TEXT"),
            ("redacted_query", "TEXT"),
            ("query_fingerprint", "TEXT"),
            ("truncated", "INTEGER NOT NULL DEFAULT 0"),
        ):
            self._ensure_column("diagnosis_tool_executions", column, definition)
        self._ensure_column("diagnosis_sdk_run_states", "decision", "TEXT")
        self._ensure_column("diagnosis_sdk_run_states", "decision_reason", "TEXT")
        self.db.execute(
            "INSERT OR IGNORE INTO buglens_schema(singleton, version) VALUES (1, ?)",
            (self.SCHEMA_VERSION,),
        )
        self.db.execute(
            "UPDATE buglens_schema SET version = ? WHERE singleton = 1",
            (self.SCHEMA_VERSION,),
        )
        self._clean_config_snapshots()
        self._backfill_state_history()
        self.db.commit()

    def _ensure_column(self, table: str, column: str, definition: str) -> None:
        columns = {
            str(row["name"])
            for row in self.db.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in columns:
            self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def _clean_config_snapshots(self) -> None:
        rows = self.db.execute(
            "SELECT snapshot_id, config_json FROM run_config_snapshots"
        ).fetchall()
        for row in rows:
            try:
                payload = json.loads(row["config_json"])
            except (TypeError, ValueError):
                continue
            cleaned = _remove_secrets(payload)
            encoded = json.dumps(cleaned, sort_keys=True, separators=(",", ":"))
            if encoded != row["config_json"]:
                self.db.execute(
                    "UPDATE run_config_snapshots SET config_json = ? WHERE snapshot_id = ?",
                    (encoded, row["snapshot_id"]),
                )

    def _backfill_state_history(self) -> None:
        rows = self.db.execute(
            "SELECT run_id, revision, state_json, updated_at FROM diagnosis_runs"
        ).fetchall()
        for row in rows:
            self.db.execute(
                """INSERT OR IGNORE INTO diagnosis_state_history
                (run_id, revision, state_json, cause_event_id, created_at)
                VALUES (?, ?, ?, NULL, ?)""",
                (row["run_id"], row["revision"], row["state_json"], row["updated_at"]),
            )

    def close(self) -> None:
        self.db.close()

    def save_config_snapshot(self, config: ResolvedRunConfig) -> None:
        payload = _remove_secrets(json.loads(config.as_json()))
        self.db.execute(
            """INSERT OR IGNORE INTO run_config_snapshots
            (snapshot_id, profile, config_version, prompt_config_version, config_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)""",
            (
                config.snapshot_id,
                config.profile,
                config.config_version,
                config.prompt_config_version,
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
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

    def save_environment_snapshot(self, snapshot: ResolvedEnvironmentSnapshot) -> None:
        payload = _remove_secrets(snapshot.model_dump(mode="json"))
        self.db.execute(
            """INSERT OR IGNORE INTO run_environment_snapshots
            (snapshot_id, environment_id, config_revision, snapshot_json, created_at)
            VALUES (?, ?, ?, ?, ?)""",
            (
                snapshot.snapshot_id,
                snapshot.environment_id,
                snapshot.config_revision,
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
                snapshot.created_at.isoformat(),
            ),
        )
        self.db.commit()

    def get_environment_snapshot(self, snapshot_id: str) -> ResolvedEnvironmentSnapshot:
        row = self.db.execute(
            "SELECT snapshot_json FROM run_environment_snapshots WHERE snapshot_id = ?",
            (snapshot_id,),
        ).fetchone()
        if row is None:
            raise RunNotFoundError(f"environment snapshot not found: {snapshot_id}")
        return ResolvedEnvironmentSnapshot.model_validate_json(row["snapshot_json"])

    def get_state(self, run_id: str) -> DiagnosisState:
        row = self.db.execute(
            "SELECT state_json FROM diagnosis_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise RunNotFoundError(f"run not found: {run_id}")
        state = DiagnosisState.model_validate_json(row["state_json"])
        control = self.db.execute(
            "SELECT cancel_requested_at, processed_at FROM diagnosis_run_control WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if control and control["cancel_requested_at"] and not control["processed_at"]:
            state = replace_state(
                state,
                cancel_requested_at=_parse_datetime(control["cancel_requested_at"]),
            )
        return state

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

    def command_run_id(self, command_id: str) -> str | None:
        row = self.db.execute(
            "SELECT run_id FROM diagnosis_commands WHERE command_id = ?",
            (command_id,),
        ).fetchone()
        return str(row["run_id"]) if row else None

    def create_run(
        self, state: DiagnosisState, events: Iterable[AgentEvent], command: AgentCommand
    ) -> list[AgentEvent]:
        event_list = list(events)
        state = self._validate_state(state)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            if self.db.execute(
                "SELECT 1 FROM diagnosis_runs WHERE run_id = ?", (state.run_id,)
            ).fetchone():
                raise RevisionConflictError("run already exists")
            normalized = self._normalize_sequences(state.run_id, event_list)
            self.db.execute(
                "INSERT INTO diagnosis_runs VALUES (?, ?, ?, ?)",
                (
                    state.run_id,
                    state.revision,
                    state.model_dump_json(),
                    state.updated_at.isoformat(),
                ),
            )
            self._insert_state_history(state, normalized)
            self._upsert_evidence(state.run_id, state)
            self._insert_events(normalized)
            self._insert_command(command, normalized)
            self.db.commit()
            return normalized
        except Exception:
            self.db.rollback()
            raise

    def commit(
        self,
        state: DiagnosisState,
        expected_revision: int,
        events: Iterable[AgentEvent],
        command: AgentCommand | None = None,
        *,
        lease_owner: str | None = None,
        fencing_token: int | None = None,
        node_execution_update: dict[str, Any] | None = None,
        clear_cancel: bool = False,
        sdk_run_state: dict[str, Any] | None = None,
        sdk_run_state_decision: dict[str, Any] | None = None,
        resolve_sdk_request_id: str | None = None,
    ) -> list[AgentEvent]:
        """Atomically save state, history, audit updates, events and command IDs."""
        state = self._validate_state(state)
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
            if state.revision != expected_revision + 1:
                raise RevisionConflictError("state revision must advance by one")
            if lease_owner is not None and fencing_token is not None:
                self._verify_lease_locked(state.run_id, lease_owner, fencing_token)
            normalized = self._normalize_sequences(state.run_id, event_list)
            self.db.execute(
                "UPDATE diagnosis_runs SET revision = ?, state_json = ?, updated_at = ? WHERE run_id = ?",
                (
                    state.revision,
                    state.model_dump_json(),
                    state.updated_at.isoformat(),
                    state.run_id,
                ),
            )
            self._insert_state_history(state, normalized)
            self._upsert_evidence(state.run_id, state)
            self._insert_events(normalized)
            if node_execution_update:
                self._update_node_execution_locked(node_execution_update)
            if sdk_run_state is not None:
                self._save_sdk_run_state_locked(sdk_run_state)
            if sdk_run_state_decision is not None:
                self._set_sdk_run_state_decision_locked(sdk_run_state_decision)
            if resolve_sdk_request_id is not None:
                self._resolve_sdk_run_state_locked(state.run_id, resolve_sdk_request_id)
            if command is not None:
                self._insert_command(command, normalized)
            if clear_cancel:
                self.db.execute(
                    "UPDATE diagnosis_run_control SET processed_at = ? WHERE run_id = ?",
                    (datetime.now(UTC).isoformat(), state.run_id),
                )
            self.db.commit()
            return normalized
        except Exception:
            self.db.rollback()
            raise

    def start_node_execution(
        self,
        state: DiagnosisState,
        expected_revision: int,
        plan: NodeExecutionPlan,
        event: AgentEvent,
        *,
        lease_owner: str,
        fencing_token: int,
        command: AgentCommand | None = None,
    ) -> list[AgentEvent]:
        """Commit the active execution and NodeAttemptStarted before external I/O."""
        state = self._validate_state(state)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute(
                "SELECT revision FROM diagnosis_runs WHERE run_id = ?", (state.run_id,)
            ).fetchone()
            if row is None:
                raise RunNotFoundError(f"run not found: {state.run_id}")
            if row["revision"] != expected_revision:
                raise RevisionConflictError("run revision changed")
            if state.revision != expected_revision + 1:
                raise RevisionConflictError("state revision must advance by one")
            self._verify_lease_locked(state.run_id, lease_owner, fencing_token)
            normalized = self._normalize_sequences(state.run_id, [event])
            self.db.execute(
                "UPDATE diagnosis_runs SET revision = ?, state_json = ?, updated_at = ? WHERE run_id = ?",
                (
                    state.revision,
                    state.model_dump_json(),
                    state.updated_at.isoformat(),
                    state.run_id,
                ),
            )
            self._insert_state_history(state, normalized)
            self._upsert_evidence(state.run_id, state)
            self._insert_events(normalized)
            if command is not None:
                self._insert_command(command, normalized)
            audit_input = _bounded_json(_audit_context(plan.input_model), 65_536)
            input_hash = hashlib.sha256(
                json.dumps(audit_input, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            self.db.execute(
                """INSERT INTO diagnosis_node_executions
                (execution_id, run_id, node, session_id, investigation_attempt,
                 clarification_round, retry_cycle, retry_index, status,
                 input_context_json, input_hash, config_snapshot_id,
                 agent_definition_version, started_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'running', ?, ?, ?, ?, ?)""",
                (
                    plan.execution_id,
                    state.run_id,
                    plan.node,
                    plan.session_id,
                    plan.investigation_attempt,
                    plan.clarification_round,
                    plan.retry_cycle,
                    plan.retry_index,
                    json.dumps(audit_input, ensure_ascii=False, separators=(",", ":")),
                    input_hash,
                    plan.config_snapshot_id,
                    plan.agent_definition_version,
                    datetime.now(UTC).isoformat(),
                ),
            )
            self.db.commit()
            return normalized
        except Exception:
            self.db.rollback()
            raise

    def events_after(
        self, run_id: str, sequence: int = 0, limit: int = 1_000
    ) -> list[AgentEvent]:
        rows = self.db.execute(
            """SELECT event_json FROM diagnosis_events
            WHERE run_id = ? AND sequence > ? ORDER BY sequence LIMIT ?""",
            (run_id, sequence, max(1, min(limit, 10_000))),
        ).fetchall()
        return [event_from_json(row["event_json"]) for row in rows]

    def current_revision(self, run_id: str) -> int:
        row = self.db.execute(
            "SELECT revision FROM diagnosis_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise RunNotFoundError(f"run not found: {run_id}")
        return int(row["revision"])

    def append_event(
        self,
        run_id: str,
        event: AgentEvent,
        command: AgentCommand | None = None,
    ) -> AgentEvent:
        """Persist a bounded audit event without changing the business revision."""
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.current_revision(run_id)
            normalized = self._normalize_sequences(run_id, [event])
            self._insert_events(normalized)
            if command is not None:
                self._insert_command(command, normalized)
            self.db.commit()
            return normalized[0]
        except Exception:
            self.db.rollback()
            raise

    def next_sequence(self, run_id: str) -> int:
        row = self.db.execute(
            "SELECT COALESCE(MAX(sequence), 0) AS sequence FROM diagnosis_events WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        return int(row["sequence"]) + 1

    def list_state_history(
        self, run_id: str, *, after_revision: int = -1, limit: int = 100
    ) -> list[DiagnosisState]:
        rows = self.db.execute(
            """SELECT state_json FROM diagnosis_state_history
            WHERE run_id = ? AND revision > ? ORDER BY revision LIMIT ?""",
            (run_id, after_revision, max(1, min(limit, 1_000))),
        ).fetchall()
        return [DiagnosisState.model_validate_json(row["state_json"]) for row in rows]

    def list_node_executions(
        self,
        *,
        run_id: str | None = None,
        node: str | None = None,
        execution_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        for name, value in (
            ("run_id", run_id),
            ("node", node),
            ("execution_id", execution_id),
            ("status", status),
        ):
            if value is not None:
                clauses.append(f"{name} = ?")
                values.append(value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        values.append(max(1, min(limit, 1_000)))
        rows = self.db.execute(
            f"SELECT * FROM diagnosis_node_executions {where} ORDER BY started_at LIMIT ?",
            values,
        ).fetchall()
        return [dict(row) for row in rows]

    def list_tool_executions(
        self,
        *,
        run_id: str | None = None,
        node_execution_id: str | None = None,
        sdk_tool_call_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        for name, value in (
            ("run_id", run_id),
            ("node_execution_id", node_execution_id),
            ("sdk_tool_call_id", sdk_tool_call_id),
        ):
            if value is not None:
                clauses.append(f"{name} = ?")
                values.append(value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        values.append(max(1, min(limit, 1_000)))
        rows = self.db.execute(
            f"SELECT * FROM diagnosis_tool_executions {where} ORDER BY started_at LIMIT ?",
            values,
        ).fetchall()
        return [dict(row) for row in rows]

    def register_evidence(self, run_id: str, evidence: EvidenceRecord) -> None:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self._insert_evidence_locked(run_id, evidence)
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def get_evidence(self, run_id: str, evidence_id: str) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT * FROM diagnosis_evidence WHERE run_id = ? AND evidence_id = ?",
            (run_id, evidence_id),
        ).fetchone()
        return dict(row) if row else None

    def list_evidence(self, run_id: str, *, limit: int = 100) -> list[EvidenceRecord]:
        rows = self.db.execute(
            "SELECT * FROM diagnosis_evidence WHERE run_id = ? "
            "ORDER BY collected_at, evidence_id LIMIT ?",
            (run_id, max(1, min(limit, 1_000))),
        ).fetchall()
        records: list[EvidenceRecord] = []
        for row in rows:
            content = row["content"] or row["storage_reference"] or row["content_hash"]
            metadata = json.loads(row["metadata_json"] or "{}")
            records.append(
                EvidenceRecord(
                    evidence_id=row["evidence_id"],
                    source=row["source_type"] or "tool",
                    source_type=row["source_type"] or "tool",
                    source_reference=row["source_reference"],
                    content=content,
                    content_hash=row["content_hash"] or "",
                    storage_reference=row["storage_reference"],
                    observed_at=row["observed_at"],
                    collected_at=_parse_datetime(row["collected_at"])
                    or datetime.now(UTC),
                    tool_execution_id=row["tool_execution_id"],
                    metadata=metadata,
                )
            )
        return records

    def start_tool_execution(self, record: dict[str, Any]) -> None:
        self.db.execute(
            """INSERT INTO diagnosis_tool_executions
            (tool_execution_id, node_execution_id, run_id, sdk_tool_call_id,
             tool_name, tool_version, plugin_id, plugin_implementation_version,
             plugin_instance_id, environment_snapshot_id, source_id, operation,
             redacted_query, query_fingerprint, retry_index, arguments_json,
             arguments_hash, status, started_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', ?)""",
            (
                record["tool_execution_id"],
                record["node_execution_id"],
                record["run_id"],
                record["sdk_tool_call_id"],
                record["tool_name"],
                record.get("tool_version", "unknown"),
                record.get("plugin_id"),
                record.get("plugin_implementation_version"),
                record.get("plugin_instance_id"),
                record.get("environment_snapshot_id"),
                record.get("source_id"),
                record.get("operation"),
                sanitize_data(record.get("redacted_query")),
                record.get("query_fingerprint"),
                record.get("retry_index", 0),
                record.get("arguments_json"),
                record.get("arguments_hash"),
                record.get("started_at", datetime.now(UTC).isoformat()),
            ),
        )
        self.db.commit()

    def finish_tool_execution(self, tool_execution_id: str, **updates: Any) -> None:
        allowed = {
            "status",
            "error_code",
            "error_message",
            "retryable",
            "result_summary_json",
            "evidence_ids_json",
            "truncated",
            "completed_at",
            "duration_ms",
        }
        values = {
            key: sanitize_data(value)
            for key, value in updates.items()
            if key in allowed
        }
        if not values:
            return
        assignments = ", ".join(f"{key} = ?" for key in values)
        self.db.execute(
            f"UPDATE diagnosis_tool_executions SET {assignments} WHERE tool_execution_id = ?",
            [*values.values(), tool_execution_id],
        )
        self.db.commit()

    def save_sdk_run_state(
        self,
        *,
        run_id: str,
        request_id: str,
        encrypted_state: str,
        sdk_version: str,
        agent_definition_version: str,
    ) -> None:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self._save_sdk_run_state_locked(
                {
                    "run_id": run_id,
                    "request_id": request_id,
                    "encrypted_state": encrypted_state,
                    "sdk_version": sdk_version,
                    "agent_definition_version": agent_definition_version,
                }
            )
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def _save_sdk_run_state_locked(self, record: dict[str, Any]) -> None:
        self.db.execute(
            """INSERT INTO diagnosis_sdk_run_states
            (run_id, request_id, encrypted_state, sdk_version,
             agent_definition_version, created_at, resolved_at, decision,
             decision_reason)
            VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, NULL)
            ON CONFLICT(run_id, request_id) DO UPDATE SET
                encrypted_state = excluded.encrypted_state,
                sdk_version = excluded.sdk_version,
                agent_definition_version = excluded.agent_definition_version,
                created_at = excluded.created_at,
                resolved_at = NULL,
                decision = NULL,
                decision_reason = NULL""",
            (
                record["run_id"],
                record["request_id"],
                record["encrypted_state"],
                record["sdk_version"],
                record["agent_definition_version"],
                datetime.now(UTC).isoformat(),
            ),
        )

    def _set_sdk_run_state_decision_locked(self, record: dict[str, Any]) -> None:
        decision = str(record.get("decision", ""))
        if decision not in {"approved", "rejected"}:
            raise ValidationFailedError("invalid SDK approval decision")
        self.db.execute(
            """UPDATE diagnosis_sdk_run_states
            SET decision = ?, decision_reason = ?
            WHERE run_id = ? AND request_id = ? AND resolved_at IS NULL""",
            (
                decision,
                str(sanitize_data(record.get("decision_reason")))
                if record.get("decision_reason") is not None
                else None,
                record["run_id"],
                record["request_id"],
            ),
        )
        if self.db.execute("SELECT changes()").fetchone()[0] != 1:
            raise ValidationFailedError("SDK approval state is unavailable")

    def _resolve_sdk_run_state_locked(self, run_id: str, request_id: str) -> None:
        self.db.execute(
            """UPDATE diagnosis_sdk_run_states SET resolved_at = ?
            WHERE run_id = ? AND request_id = ? AND resolved_at IS NULL""",
            (datetime.now(UTC).isoformat(), run_id, request_id),
        )

    def set_sdk_run_state_decision(
        self,
        *,
        run_id: str,
        request_id: str,
        decision: str,
        decision_reason: str | None = None,
    ) -> None:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self._set_sdk_run_state_decision_locked(
                {
                    "run_id": run_id,
                    "request_id": request_id,
                    "decision": decision,
                    "decision_reason": decision_reason,
                }
            )
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def resolve_sdk_run_state(self, run_id: str, request_id: str) -> None:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self._resolve_sdk_run_state_locked(run_id, request_id)
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def get_sdk_run_state(self, run_id: str, request_id: str) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT * FROM diagnosis_sdk_run_states WHERE run_id = ? AND request_id = ?",
            (run_id, request_id),
        ).fetchone()
        return dict(row) if row else None

    def has_unresolved_sdk_decision(self, run_id: str) -> bool:
        """Return whether a committed approval decision still needs SDK replay."""
        row = self.db.execute(
            """SELECT 1 FROM diagnosis_sdk_run_states
            WHERE run_id = ? AND decision IS NOT NULL AND resolved_at IS NULL
            LIMIT 1""",
            (run_id,),
        ).fetchone()
        return row is not None

    def request_cancel(
        self,
        run_id: str,
        expected_revision: int,
        command: AgentCommand,
        event: AgentEvent,
    ) -> AgentEvent:
        """Accept Cancel without taking the active node's lease or revision."""
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute(
                "SELECT revision, state_json FROM diagnosis_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise RunNotFoundError(f"run not found: {run_id}")
            if row["revision"] != expected_revision:
                raise RevisionConflictError(
                    "expected_revision does not match run revision"
                )
            state = DiagnosisState.model_validate_json(row["state_json"])
            if state.lifecycle_status.value in {"completed", "canceled"}:
                raise InvalidRunStatusError("run is already terminal")
            normalized = self._normalize_sequences(run_id, [event])
            now = datetime.now(UTC).isoformat()
            reason = str(sanitize_data(getattr(command, "reason", "")))
            self.db.execute(
                """INSERT INTO diagnosis_run_control
                (run_id, cancel_requested_at, cancel_command_id, reason, processed_at)
                VALUES (?, ?, ?, ?, NULL)
                ON CONFLICT(run_id) DO UPDATE SET
                    cancel_requested_at = excluded.cancel_requested_at,
                    cancel_command_id = excluded.cancel_command_id,
                    reason = excluded.reason,
                    processed_at = NULL""",
                (run_id, now, command.command_id, reason),
            )
            self._insert_events(normalized)
            self._insert_command(command, normalized)
            self.db.commit()
            return normalized[0]
        except Exception:
            self.db.rollback()
            raise

    def is_cancel_requested(self, run_id: str) -> bool:
        row = self.db.execute(
            "SELECT 1 FROM diagnosis_run_control WHERE run_id = ? AND cancel_requested_at IS NOT NULL AND processed_at IS NULL",
            (run_id,),
        ).fetchone()
        return row is not None

    def cancel_reason(self, run_id: str) -> str:
        row = self.db.execute(
            "SELECT reason FROM diagnosis_run_control WHERE run_id = ? AND processed_at IS NULL",
            (run_id,),
        ).fetchone()
        return str(row["reason"] or "cancel requested") if row else "cancel requested"

    def acquire_lease(self, run_id: str, owner: str, seconds: int = 60) -> int:
        now = time.time()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute(
                "SELECT owner, fencing_token, expires_at FROM run_leases WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row and row["expires_at"] > now and row["owner"] != owner:
                raise LeaseConflictError("run is already being processed")
            token = int(time.time_ns() & 0x7FFFFFFFFFFFFFFF)
            self.db.execute(
                """INSERT INTO run_leases(run_id, owner, fencing_token, expires_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET owner=excluded.owner,
                    fencing_token=excluded.fencing_token, expires_at=excluded.expires_at""",
                (run_id, owner, token, now + seconds),
            )
            self.db.commit()
            return token
        except Exception:
            self.db.rollback()
            raise

    def lease_active(self, run_id: str) -> bool:
        """Return whether another executor currently owns a non-expired lease."""
        row = self.db.execute(
            "SELECT expires_at FROM run_leases WHERE run_id = ?", (run_id,)
        ).fetchone()
        return row is not None and float(row["expires_at"]) > time.time()

    def renew_lease(
        self, run_id: str, owner: str, fencing_token: int, seconds: int = 60
    ) -> None:
        result = self.db.execute(
            """UPDATE run_leases SET expires_at = ?
            WHERE run_id = ? AND owner = ? AND fencing_token = ? AND expires_at > ?""",
            (time.time() + seconds, run_id, owner, fencing_token, time.time()),
        )
        self.db.commit()
        if result.rowcount != 1:
            raise FencingTokenError("lease is no longer owned by this executor")

    def verify_lease(self, run_id: str, owner: str, fencing_token: int) -> None:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self._verify_lease_locked(run_id, owner, fencing_token)
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def _verify_lease_locked(self, run_id: str, owner: str, fencing_token: int) -> None:
        row = self.db.execute(
            "SELECT owner, fencing_token, expires_at FROM run_leases WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if (
            row is None
            or row["owner"] != owner
            or row["fencing_token"] != fencing_token
            or row["expires_at"] <= time.time()
        ):
            raise FencingTokenError("lease is no longer owned by this executor")

    def release_lease(
        self, run_id: str, owner: str, fencing_token: int | None = None
    ) -> None:
        if fencing_token is None:
            self.db.execute(
                "DELETE FROM run_leases WHERE run_id = ? AND owner = ?",
                (run_id, owner),
            )
        else:
            self.db.execute(
                "DELETE FROM run_leases WHERE run_id = ? AND owner = ? AND fencing_token = ?",
                (run_id, owner, fencing_token),
            )
        self.db.commit()

    def recover_interrupted_executions(self, run_id: str) -> list[dict[str, Any]]:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            rows = self.db.execute(
                "SELECT * FROM diagnosis_node_executions WHERE run_id = ? AND status = 'running'",
                (run_id,),
            ).fetchall()
            if not rows:
                self.db.commit()
                return []
            now = datetime.now(UTC).isoformat()
            self.db.execute(
                """UPDATE diagnosis_node_executions SET status = 'interrupted',
                error_code = 'process_interrupted', error_message = ?, retryable = 1,
                completed_at = ? WHERE run_id = ? AND status = 'running'""",
                ("node execution was interrupted before completion", now, run_id),
            )
            execution_ids = [str(row["execution_id"]) for row in rows]
            placeholders = ",".join("?" for _ in execution_ids)
            self.db.execute(
                f"""UPDATE diagnosis_tool_executions SET status = 'interrupted',
                error_code = 'process_interrupted',
                error_message = ?, retryable = 1, completed_at = ?
                WHERE node_execution_id IN ({placeholders}) AND status = 'running'""",
                [
                    "tool execution was interrupted before completion",
                    now,
                    *execution_ids,
                ],
            )
            self.db.commit()
            return [dict(row) for row in rows]
        except Exception:
            self.db.rollback()
            raise

    def session_exists(self, session_id: str) -> bool:
        try:
            row = self.db.execute(
                "SELECT 1 FROM agent_sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        except sqlite3.OperationalError:
            return False
        return row is not None

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
        rows = self.db.execute(
            "SELECT state_json FROM diagnosis_runs ORDER BY updated_at DESC, run_id DESC"
        ).fetchall()
        normalized_query = query.strip().lower() if query else None
        matches: list[DiagnosisState] = []
        degraded_count = 0
        for row in rows:
            try:
                state = DiagnosisState.model_validate_json(row["state_json"])
            except (TypeError, ValueError, json.JSONDecodeError):
                # One legacy/corrupt row must not make the whole control-plane
                # listing unavailable.  AdminApplicationService exposes the
                # count so operators can investigate the degraded rows.
                degraded_count += 1
                continue
            if lifecycle_status and state.lifecycle_status.value != lifecycle_status:
                continue
            if outcome and (state.outcome is None or state.outcome.value != outcome):
                continue
            if current_node and state.current_node.value != current_node:
                continue
            if profile and state.config_profile != profile:
                continue
            if normalized_query:
                haystack = f"{state.run_id} {state.user_question}".lower()
                if normalized_query not in haystack:
                    continue
            matches.append(state)
        self.last_list_degraded_count = degraded_count
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
        degraded_count = 0
        for row in self.db.execute("SELECT state_json FROM diagnosis_runs").fetchall():
            try:
                state = DiagnosisState.model_validate_json(row["state_json"])
            except (TypeError, ValueError, json.JSONDecodeError):
                degraded_count += 1
                continue
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
            elif state.lifecycle_status.value in {
                "waiting_user",
                "waiting_approval",
                "waiting_tool",
                "waiting_for_target_confirmation",
            }:
                session_status = "waiting"
            elif state.lifecycle_status.value in {"completed", "failed", "canceled"}:
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
        self.last_list_degraded_count = degraded_count
        return records[start:end], len(records)

    def _validate_state(self, state: DiagnosisState) -> DiagnosisState:
        # Full round trip catches nested in-place mutations before SQLite sees them.
        return DiagnosisState.model_validate(
            sanitize_data(state.model_dump(mode="python"))
        )

    def _normalize_sequences(
        self, run_id: str, events: list[AgentEvent]
    ) -> list[AgentEvent]:
        if not events:
            return []
        current = self.db.execute(
            "SELECT COALESCE(MAX(sequence), 0) AS sequence FROM diagnosis_events WHERE run_id = ?",
            (run_id,),
        ).fetchone()["sequence"]
        normalized: list[AgentEvent] = []
        for index, event in enumerate(events, start=1):
            copied = event.model_copy(update={"sequence": int(current) + index})
            excluded = {
                "protocol_version",
                "event_id",
                "run_id",
                "sequence",
                "revision",
                "occurred_at",
                "specversion",
                "id",
                "source",
                "subject",
                "type",
                "time",
                "datacontenttype",
                "data",
                "runid",
            }
            object.__setattr__(
                copied,
                "data",
                copied.model_dump(mode="json", exclude=excluded),
            )
            normalized.append(copied)
        return normalized

    def _insert_state_history(
        self, state: DiagnosisState, events: list[AgentEvent]
    ) -> None:
        cause_event_id = events[0].event_id if events else None
        self.db.execute(
            """INSERT OR REPLACE INTO diagnosis_state_history
            (run_id, revision, state_json, cause_event_id, created_at)
            VALUES (?, ?, ?, ?, ?)""",
            (
                state.run_id,
                state.revision,
                state.model_dump_json(),
                cause_event_id,
                state.updated_at.isoformat(),
            ),
        )

    def _upsert_evidence(self, run_id: str, state: DiagnosisState) -> None:
        records: dict[str, EvidenceRecord] = {}
        for record in state.source_evidence:
            records[record.evidence_id] = record
        if state.analysis is not None:
            for record in state.analysis.extracted_evidence:
                records[record.evidence_id] = record
        for answer in state.answers:
            for record in answer.attachments:
                records[record.evidence_id] = record
        for record in records.values():
            self._insert_evidence_locked(run_id, record)

    def _insert_evidence_locked(self, run_id: str, evidence: EvidenceRecord) -> None:
        clean_data = dict(sanitize_data(evidence.model_dump(mode="python")))
        clean_data["content_hash"] = ""
        evidence = EvidenceRecord.model_validate(clean_data)
        existing = self.db.execute(
            "SELECT run_id, content_hash FROM diagnosis_evidence WHERE evidence_id = ?",
            (evidence.evidence_id,),
        ).fetchone()
        if existing is not None and existing["run_id"] != run_id:
            raise ValidationFailedError(
                f"evidence id {evidence.evidence_id!r} is already registered for another run"
            )
        if (
            existing is not None
            and existing["content_hash"]
            and existing["content_hash"] != evidence.content_hash
        ):
            raise ValidationFailedError(
                f"evidence id {evidence.evidence_id!r} is immutable"
            )
        content = evidence.content
        if len(content.encode("utf-8")) > 65_536:
            content = content[:16_000]
        self.db.execute(
            """INSERT OR REPLACE INTO diagnosis_evidence
            (evidence_id, run_id, source_type, source_reference, content_hash,
             content, storage_reference, observed_at, collected_at,
             tool_execution_id, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                evidence.evidence_id,
                run_id,
                evidence.source_type,
                evidence.source_reference,
                evidence.content_hash,
                content,
                evidence.storage_reference,
                evidence.observed_at,
                evidence.collected_at.isoformat(),
                evidence.tool_execution_id,
                json.dumps(_bounded_json(evidence.metadata, 4_096), ensure_ascii=False),
            ),
        )

    def _update_node_execution_locked(self, update: dict[str, Any]) -> None:
        execution_id = update.get("execution_id")
        if not execution_id:
            return
        allowed = {
            "status",
            "output_json",
            "error_code",
            "error_message",
            "retryable",
            "reasoning_summary_json",
            "trace_id",
            "completed_at",
        }
        values = {
            key: sanitize_data(value) for key, value in update.items() if key in allowed
        }
        if not values:
            return
        assignments = ", ".join(f"{key} = ?" for key in values)
        self.db.execute(
            f"UPDATE diagnosis_node_executions SET {assignments} WHERE execution_id = ?",
            [*values.values(), execution_id],
        )

    def _insert_events(self, events: list[AgentEvent]) -> None:
        for event in events:
            payload = _bounded_json(event, 65_536)
            self.db.execute(
                "INSERT INTO diagnosis_events VALUES (?, ?, ?, ?, ?, ?)",
                (
                    event.event_id,
                    event.run_id,
                    event.sequence,
                    event.revision,
                    event.event_type,
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                ),
            )

    def _insert_command(self, command: AgentCommand, events: list[AgentEvent]) -> None:
        existing = self.db.execute(
            "SELECT run_id, event_ids_json FROM diagnosis_commands WHERE command_id = ?",
            (command.command_id,),
        ).fetchone()
        if existing is not None and existing["run_id"] != command.run_id:
            raise ValidationFailedError("command_id is already bound to another run")
        current_ids = json.loads(existing["event_ids_json"]) if existing else []
        for event in events:
            if event.event_id not in current_ids:
                current_ids.append(event.event_id)
        command_payload = _bounded_json(command, 65_536)
        self.db.execute(
            """INSERT INTO diagnosis_commands
            (command_id, run_id, event_ids_json, command_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(command_id) DO UPDATE SET
                event_ids_json = excluded.event_ids_json""",
            (
                command.command_id,
                command.run_id,
                json.dumps(current_ids),
                json.dumps(command_payload, ensure_ascii=False, separators=(",", ":")),
                datetime.now(UTC).isoformat(),
            ),
        )


def _remove_secrets(value: Any) -> Any:
    secret_keys = {
        "api_key",
        "apikey",
        "authorization",
        "cookie",
        "username",
        "password",
        "secret",
        "secret_key",
        "token",
    }
    if isinstance(value, dict):
        return {
            key: _remove_secrets(item)
            for key, item in value.items()
            if str(key).lower() not in secret_keys
        }
    if isinstance(value, list):
        return [_remove_secrets(item) for item in value]
    return value


def _bounded_json(value: Any, max_bytes: int) -> Any:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    elif isinstance(value, datetime):
        value = value.isoformat()
    value = sanitize_data(value)
    if isinstance(value, dict):
        value = {key: _bounded_json(item, max_bytes) for key, item in value.items()}
    elif isinstance(value, list):
        value = [_bounded_json(item, max_bytes) for item in value[:100]]
    encoded = json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))
    if len(encoded.encode("utf-8")) <= max_bytes:
        return value
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return {"truncated": True, "sha256": digest, "bytes": len(encoded.encode("utf-8"))}


def _audit_context(value: Any) -> Any:
    """Keep evidence references in node audits without copying full evidence."""
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if isinstance(value, dict):
        if "evidence_id" in value and "content" in value:
            content = str(value.get("content") or "")
            return {
                key: value.get(key)
                for key in (
                    "evidence_id",
                    "source",
                    "source_type",
                    "source_reference",
                    "content_hash",
                    "storage_reference",
                    "observed_at",
                    "collected_at",
                )
                if value.get(key) is not None
            } | {"summary": content[:512]}
        return {key: _audit_context(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_audit_context(item) for item in value[:100]]
    if isinstance(value, tuple):
        return [_audit_context(item) for item in value[:100]]
    return value


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def event_from_json(value: str) -> AgentEvent:
    from .protocol.events import (
        InputRequired,
        InputSkipped,
        NodeAttemptFailed,
        NodeAttemptStarted,
        NodeCompleted,
        NodeRetryScheduled,
        NodeStarted,
        RunCanceled,
        RunCancelRequested,
        RunCompleted,
        RunFailed,
        RunResumeAvailable,
        RunResumed,
        RunStarted,
        RunWaiting,
        TargetConfirmationRequired,
        TargetConfirmed,
        ToolApprovalRequired,
        ToolApprovalResolved,
        ToolCallCompleted,
        ToolCallFailed,
        ToolCallStarted,
        UserInputSubmitted,
    )

    classes = {
        "run_started": RunStarted,
        "node_attempt_started": NodeAttemptStarted,
        "node_started": NodeStarted,
        "node_retry_scheduled": NodeRetryScheduled,
        "node_attempt_failed": NodeAttemptFailed,
        "node_completed": NodeCompleted,
        "tool_call_started": ToolCallStarted,
        "tool_call_completed": ToolCallCompleted,
        "tool_call_failed": ToolCallFailed,
        "target_confirmation_required": TargetConfirmationRequired,
        "target_confirmed": TargetConfirmed,
        "tool_approval_required": ToolApprovalRequired,
        "tool_approval_resolved": ToolApprovalResolved,
        "input_required": InputRequired,
        "input_skipped": InputSkipped,
        "run_waiting": RunWaiting,
        "run_resume_available": RunResumeAvailable,
        "run_resumed": RunResumed,
        "run_cancel_requested": RunCancelRequested,
        "user_input_submitted": UserInputSubmitted,
        "run_completed": RunCompleted,
        "run_failed": RunFailed,
        "run_canceled": RunCanceled,
    }
    data = json.loads(value)
    event_type = data.get("event_type")
    if event_type is None:
        event_type = str(data.get("type", "")).rsplit(".", 1)[-1]
    cls = classes.get(event_type)
    if cls is None:
        raise ValidationFailedError(f"unknown event type: {event_type}")
    return cls.model_validate(data)

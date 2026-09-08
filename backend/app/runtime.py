"""Shared short-lived Agent Runtime used by local and remote adapters."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import AsyncIterator, Literal, Protocol
from uuid import uuid4

from agents import trace
from pydantic import ValidationError

from .agents import NodeApprovalRequired, NodeExecutionError
from .config import ConfigRepository, ResolvedRunConfig
from .graph import DiagnosisGraph
from .infra import (
    FencingTokenError,
    InvalidRunStatusError,
    LeaseConflictError,
    PendingRequestMismatchError,
    RevisionConflictError,
    RunNotFoundError,
    SQLiteCheckpointStore,
    ValidationFailedError,
)
from .models import (
    DiagnosisOutcome,
    DiagnosisState,
    ExecutionFailure,
    FailureRecord,
    GraphNode,
    LifecycleStatus,
    PendingApproval,
    replace_state,
)
from .protocol.commands import (
    AgentCommand,
    ApproveTool,
    CancelDiagnosis,
    RejectTool,
    ResumeDiagnosis,
    SkipUserInteraction,
    StartDiagnosis,
    SubmitUserAnswers,
)
from .protocol.events import (
    AgentEvent,
    InputRequired,
    InputSkipped,
    NodeAttemptFailed,
    NodeAttemptStarted,
    NodeCompleted,
    NodeRetryScheduled,
    RunCanceled,
    RunCancelRequested,
    RunCompleted,
    RunFailed,
    RunResumeAvailable,
    RunResumed,
    RunStarted,
    RunWaiting,
    ToolApprovalRequired,
    ToolApprovalResolved,
    ToolCallCompleted,
    ToolCallFailed,
    ToolCallStarted,
    UserInputSubmitted,
)
from .security import RunStateCipherError, SDKRunStateCipher, sanitize_data


@dataclass(frozen=True)
class _ApprovalResume:
    request_id: str
    state_string: str
    decision: Literal["approved", "rejected"]
    tool_call_ids: list[str]


class _StoreToolObserver:
    """Synchronous bridge used by SDK hooks while a node is in flight."""

    def __init__(
        self,
        store: SQLiteCheckpointStore,
        run_id: str,
        command: AgentCommand | None = None,
    ) -> None:
        self.store = store
        self.run_id = run_id
        self.command = command
        self.evidence: list = []
        self.reasoning_summary: list[str] = []
        self._tool_records: dict[str, dict] = {}

    def reasoning_captured(self, summaries: list[str]) -> None:
        self.reasoning_summary = [str(item)[:2_000] for item in summaries[:20]]

    def reasoning_summary_json(self) -> str | None:
        if not self.reasoning_summary:
            return None
        return json.dumps(
            sanitize_data(self.reasoning_summary),
            ensure_ascii=False,
            separators=(",", ":"),
        )

    def tool_started(self, **record) -> None:
        self.store.start_tool_execution(record)
        tool_execution_id = str(record["tool_execution_id"])
        self._tool_records[tool_execution_id] = record
        self.store.append_event(
            self.run_id,
            ToolCallStarted(
                run_id=self.run_id,
                sequence=1,
                revision=self.store.current_revision(self.run_id),
                node_execution_id=str(record["node_execution_id"]),
                tool_execution_id=tool_execution_id,
                sdk_tool_call_id=str(record["sdk_tool_call_id"]),
                tool_name=str(record["tool_name"]),
            ),
            self.command,
        )

    def tool_finished(self, tool_execution_id: str, **updates) -> None:
        self.store.finish_tool_execution(tool_execution_id, **updates)
        record = self._tool_records.pop(tool_execution_id, {})
        base = {
            "run_id": self.run_id,
            "sequence": 1,
            "revision": self.store.current_revision(self.run_id),
            "node_execution_id": str(record.get("node_execution_id", "unknown")),
            "tool_execution_id": tool_execution_id,
            "sdk_tool_call_id": str(record.get("sdk_tool_call_id", "unknown")),
            "tool_name": str(record.get("tool_name", "unknown")),
        }
        if updates.get("status") == "succeeded":
            evidence_ids = json.loads(updates.get("evidence_ids_json", "[]"))
            event = ToolCallCompleted(
                **base,
                evidence_ids=evidence_ids,
                duration_ms=updates.get("duration_ms"),
            )
        else:
            failure = ExecutionFailure(
                code=str(updates.get("error_code", "tool_execution_failed")),
                message="read-only tool execution failed",
                retryable=bool(updates.get("retryable", False)),
            )
            event = ToolCallFailed(**base, failure=failure)
        self.store.append_event(self.run_id, event, self.command)

    def evidence_registered(self, evidence) -> None:
        self.store.register_evidence(self.run_id, evidence)
        self.evidence.append(evidence)


class RunView(DiagnosisState):
    """Read model returned to adapters; it contains no SDK session details."""


class AgentRuntime(Protocol):
    def run(self, command: AgentCommand) -> AsyncIterator[AgentEvent]: ...

    async def get_run(self, run_id: str) -> RunView: ...


class DiagnosisRuntime:
    """Own persistence, leases and execution boundaries around the pure Graph."""

    def __init__(
        self,
        graph: DiagnosisGraph,
        store: SQLiteCheckpointStore,
        configs: ConfigRepository,
        *,
        tracing_enabled: bool = True,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        run_state_cipher: SDKRunStateCipher | None = None,
    ) -> None:
        self.graph = graph
        self.store = store
        self.configs = configs
        self.tracing_enabled = tracing_enabled
        self._sleep = sleep or asyncio.sleep
        self.run_state_cipher = run_state_cipher
        # Keep the resolved credential-bearing object in memory for waiting and
        # Resume commands in this process. The SQLite snapshot remains
        # credential-free; a new process must obtain credentials from its
        # configured provider/environment rather than from the checkpoint.
        self._config_cache: dict[str, ResolvedRunConfig] = {}
        # Kept for compatibility with callers that displayed the old owner.  A
        # new command below receives its own owner/fencing token.
        self.owner = uuid4().hex

    async def run(
        self,
        command: AgentCommand,
        resolved_config: ResolvedRunConfig | None = None,
    ) -> AsyncIterator[AgentEvent]:
        existing_run_id = self.store.command_run_id(command.command_id)
        if existing_run_id is not None and existing_run_id != command.run_id:
            raise ValidationFailedError("command_id is already bound to another run")
        previous = self.store.get_command(command.command_id)
        approval_replay = False
        if previous is not None:
            # A command row is created before the first external call.  If the
            # process died in that small window, replaying only the stored
            # RunStarted/InputSubmitted event would strand a still-running run.
            # Continue only when the command was committed but no node call was
            # marked active; an active execution is recovered separately and is
            # never replayed automatically.
            if isinstance(command, CancelDiagnosis):
                for event in previous:
                    yield event
                return
            replay_state = self.store.get_state(command.run_id)
            replay_config = self._config_snapshot(replay_state.config_snapshot_id)
            replay_state = self._recover_stale_run(replay_state, replay_config)
            approval_replay = isinstance(command, (ApproveTool, RejectTool)) and (
                replay_state.lifecycle_status == LifecycleStatus.RUNNING
                and replay_state.active_execution_id is not None
                and self._approval_decision_matches(command)
            )
            if approval_replay:
                # The decision commit may have succeeded immediately before a
                # worker crashed. Re-enter the existing SDK execution exactly
                # once using its unresolved decision row.
                pass
            elif (
                replay_state.lifecycle_status != LifecycleStatus.RUNNING
                or replay_state.active_execution_id is not None
            ):
                for event in previous:
                    yield event
                return

        if isinstance(command, CancelDiagnosis):
            async for event in self._run_cancel(command):
                yield event
            return

        state: DiagnosisState | None = None
        config: ResolvedRunConfig
        initial_events: list[AgentEvent] = []
        command_already_committed = False
        replaying_committed_command = previous is not None
        approval_resume: _ApprovalResume | None = None
        if isinstance(command, StartDiagnosis) and not replaying_committed_command:
            if command.expected_revision is not None:
                raise ValidationFailedError(
                    "start command cannot set expected_revision"
                )
            config = resolved_config or self.configs.resolve(command.profile)
            self.store.save_config_snapshot(config)
            self._config_cache[config.snapshot_id] = config
            state = DiagnosisState.create(
                command.question,
                command.context,
                command.evidence,
                config.policy.graph.max_investigation_attempts,
                config.policy.graph.max_clarification_rounds,
                config_snapshot_id=config.snapshot_id,
                profile=config.profile,
                config_version=config.config_version,
            )
            state = replace_state(
                state,
                run_id=command.run_id,
                lifecycle_status=LifecycleStatus.RUNNING,
            )
            started = self._event(
                RunStarted,
                state,
                profile=config.profile,
                config_snapshot_id=config.snapshot_id,
                config_version=config.config_version,
                revision=state.revision,
            )
            initial_events = self.store.create_run(state, [started], command)
            command_already_committed = True
        else:
            state = self.store.get_state(command.run_id)
            config = self._config_snapshot(state.config_snapshot_id)
            state = self._recover_stale_run(state, config)
            state = self._hydrate_evidence(state)
            if not replaying_committed_command:
                self._validate_command(command, state)
            else:
                initial_events = list(previous or [])
                command_already_committed = True
                if isinstance(command, (ApproveTool, RejectTool)):
                    approval_resume = self._load_approval_resume(
                        command, expected_decision=self._approval_decision(command)
                    )

        owner = uuid4().hex
        token: int | None = None
        try:
            token = self.store.acquire_lease(
                command.run_id,
                owner,
                config.policy.lease_seconds,
            )
            for event in initial_events:
                yield event

            if (
                isinstance(command, (SubmitUserAnswers, SkipUserInteraction))
                and not replaying_committed_command
            ):
                state, input_event = self._apply_user_command(state, command)
                committed = self.store.commit(
                    state,
                    state.revision - 1,
                    [input_event],
                    command,
                    lease_owner=owner,
                    fencing_token=token,
                )
                for event in committed:
                    yield event
                command_already_committed = True
            elif (
                isinstance(command, ResumeDiagnosis) and not replaying_committed_command
            ):
                self._check_resume_compatibility(state)
                state = replace_state(
                    state,
                    lifecycle_status=LifecycleStatus.RUNNING,
                    retry_cycle=state.retry_cycle + 1,
                    retry_index=0,
                    resume_available=False,
                    last_error=None,
                    active_execution_id=None,
                    revision=state.revision + 1,
                    updated_at=datetime.now(UTC),
                )
                resumed = self._event(
                    RunResumed,
                    state,
                    node=state.current_node.value,
                    retry_cycle=state.retry_cycle,
                )
                committed = self.store.commit(
                    state,
                    state.revision - 1,
                    [resumed],
                    command,
                    lease_owner=owner,
                    fencing_token=token,
                )
                for event in committed:
                    yield event
                command_already_committed = True
            elif isinstance(command, (ApproveTool, RejectTool)) and not approval_replay:
                approval_resume = self._load_approval_resume(
                    command, expected_decision=self._approval_decision(command)
                )
                resumed_state = replace_state(
                    state,
                    lifecycle_status=LifecycleStatus.RUNNING,
                    pending_approval=None,
                    last_error=None,
                    resume_available=False,
                    revision=state.revision + 1,
                    updated_at=datetime.now(UTC),
                )
                resolved = self._event(
                    ToolApprovalResolved,
                    resumed_state,
                    request_id=command.request_id,
                    decision=approval_resume.decision,
                )
                committed = self.store.commit(
                    resumed_state,
                    state.revision,
                    [resolved],
                    command,
                    lease_owner=owner,
                    fencing_token=token,
                    sdk_run_state_decision={
                        "run_id": command.run_id,
                        "request_id": command.request_id,
                        "decision": approval_resume.decision,
                        "decision_reason": (
                            "user approved tool execution"
                            if approval_resume.decision == "approved"
                            else "user rejected tool execution"
                        ),
                    },
                )
                state = resumed_state
                for event in committed:
                    yield event
                command_already_committed = True

            if (
                isinstance(command, (ApproveTool, RejectTool))
                and approval_resume is None
            ):
                raise ValidationFailedError(
                    "tool approval state is unavailable for this command"
                )

            async for event in self._drive(
                state,
                config,
                command,
                owner,
                token,
                approval_resume=approval_resume,
            ):
                yield event
        except (
            RunNotFoundError,
            RevisionConflictError,
            InvalidRunStatusError,
            PendingRequestMismatchError,
            ValidationFailedError,
            LeaseConflictError,
            FencingTokenError,
        ):
            raise
        except Exception as exc:
            execution_failure = self._execution_failure(exc)
            logging.error(
                "Runtime execution failed for run %s at node %s",
                state.run_id if state is not None else command.run_id,
                state.current_node if state is not None else None,
                extra={"failure_code": execution_failure.code},
            )
            if (
                token is not None
                and state is not None
                and state.lifecycle_status
                not in {
                    LifecycleStatus.COMPLETED,
                    LifecycleStatus.CANCELED,
                }
            ):
                failure = self._failure_from_exception(exc, state)
                failed = replace_state(
                    state,
                    lifecycle_status=LifecycleStatus.FAILED,
                    last_error=failure,
                    resume_available=failure.retryable,
                    active_execution_id=None,
                    revision=state.revision + 1,
                    updated_at=datetime.now(UTC),
                )
                failed_event = self._event(RunFailed, failed, failure=failure)
                committed = self.store.commit(
                    failed,
                    state.revision,
                    [failed_event],
                    command if command_already_committed is False else command,
                    lease_owner=owner,
                    fencing_token=token,
                )
                for event in committed:
                    yield event
            else:
                raise
        finally:
            if token is not None:
                self.store.release_lease(command.run_id, owner, token)

    async def _drive(
        self,
        state: DiagnosisState,
        config: ResolvedRunConfig,
        command: AgentCommand,
        owner: str,
        fencing_token: int,
        *,
        approval_resume: _ApprovalResume | None = None,
    ) -> AsyncIterator[AgentEvent]:
        last_renewal = time.monotonic()
        with trace(
            "BugLens diagnosis",
            group_id=state.run_id,
            metadata={
                "snapshot_id": config.snapshot_id,
                "profile": config.profile,
                "config_version": config.config_version,
                "prompt_config_version": config.prompt_config_version,
            },
            disabled=not self.tracing_enabled,
        ):
            while state.lifecycle_status == LifecycleStatus.RUNNING:
                if self.store.is_cancel_requested(state.run_id):
                    state, events = self._cancel_at_boundary(
                        state, self.store.cancel_reason(state.run_id)
                    )
                    committed = self.store.commit(
                        state,
                        state.revision - 1,
                        events,
                        command,
                        lease_owner=owner,
                        fencing_token=fencing_token,
                        clear_cancel=True,
                    )
                    for event in committed:
                        yield event
                    break

                if (
                    time.monotonic() - last_renewal
                    >= config.policy.lease_renewal_seconds
                ):
                    self.store.renew_lease(
                        state.run_id,
                        owner,
                        fencing_token,
                        config.policy.lease_seconds,
                    )
                    last_renewal = time.monotonic()

                resume_for_call = approval_resume
                approval_resume = None
                plan = self.graph.prepare_step(state, config)
                if resume_for_call is not None:
                    if not state.active_execution_id:
                        raise ValidationFailedError(
                            "approval resume has no active node execution"
                        )
                    plan = plan.model_copy(
                        update={"execution_id": state.active_execution_id}
                    )
                    start_revision = state.revision
                    start_state = state
                else:
                    start_revision = state.revision
                    start_state = replace_state(
                        state,
                        active_execution_id=plan.execution_id,
                        retry_cycle=plan.retry_cycle,
                        retry_index=plan.retry_index,
                        revision=start_revision + 1,
                        updated_at=datetime.now(UTC),
                    )
                    started = self._event(
                        NodeAttemptStarted,
                        start_state,
                        node=plan.node,
                        execution_id=plan.execution_id,
                        session_id=plan.session_id,
                        investigation_attempt=plan.investigation_attempt,
                        clarification_round=plan.clarification_round,
                        retry_cycle=plan.retry_cycle,
                        retry_index=plan.retry_index,
                    )
                    started_events = self.store.start_node_execution(
                        start_state,
                        start_revision,
                        plan,
                        started,
                        lease_owner=owner,
                        fencing_token=fencing_token,
                        command=command,
                    )
                    state = start_state
                    for event in started_events:
                        yield event

                runtime = self.graph._runtime_for_config(state, plan.node, config)
                observer = _StoreToolObserver(self.store, state.run_id, command)
                runtime = replace(
                    runtime,
                    execution_id=plan.execution_id,
                    tool_registry=getattr(self.graph.runner, "tool_registry", None),
                    execution_observer=observer,
                )
                renewal_stop = asyncio.Event()
                renewal_failure: list[Exception] = []
                renewal_task = asyncio.create_task(
                    self._renew_lease_during_call(
                        state.run_id,
                        owner,
                        fencing_token,
                        config,
                        renewal_stop,
                        renewal_failure,
                    )
                )
                try:
                    category = (
                        state.analysis.category
                        if state.analysis is not None and plan.node == "investigate"
                        else None
                    )
                    if resume_for_call is not None:
                        resume_runner = getattr(
                            self.graph.runner, "resume_approval", None
                        )
                        if not callable(resume_runner):
                            raise NodeExecutionError(
                                "the configured node runner cannot resume approvals",
                                code="sdk_run_state_unavailable",
                                retryable=False,
                                auto_retry=False,
                            )
                        result = await resume_runner(
                            plan.node,
                            plan.input_model,
                            runtime,
                            state_string=resume_for_call.state_string,
                            decision=resume_for_call.decision,
                            expected_tool_call_ids=resume_for_call.tool_call_ids,
                            category=category,
                        )
                    else:
                        result = await self.graph.runner.run(
                            plan.node,
                            plan.input_model,
                            runtime,
                            category,
                        )
                    if plan.node == "evaluate":
                        new_state, transition = self.graph.apply_evaluation_policy(
                            state,
                            plan,
                            result,
                            config,
                            registered_evidence_ids={
                                item.evidence_id for item in observer.evidence
                            },
                        )
                    else:
                        new_state, transition = self.graph.apply_result(
                            state,
                            plan,
                            result,
                            registered_evidence_ids={
                                item.evidence_id for item in observer.evidence
                            },
                        )
                    if observer.evidence:
                        existing = {
                            item.evidence_id for item in new_state.source_evidence
                        }
                        new_state = replace_state(
                            new_state,
                            source_evidence=[
                                *new_state.source_evidence,
                                *[
                                    item
                                    for item in observer.evidence
                                    if item.evidence_id not in existing
                                ],
                            ][:100],
                        )
                    if renewal_failure:
                        raise renewal_failure[0]
                except NodeApprovalRequired as approval:
                    try:
                        if self.run_state_cipher is None:
                            raise RunStateCipherError(
                                "SDK approval state encryption is not configured"
                            )
                        if len(approval.state_string.encode("utf-8")) > 4_000_000:
                            raise RunStateCipherError(
                                "SDK approval state exceeds the durable size limit"
                            )
                        pending = PendingApproval(
                            request_id=approval.request_id,
                            tool_name=approval.tool_name,
                            explanation=approval.explanation,
                            tool_call_ids=approval.tool_call_ids,
                            arguments=approval.arguments,
                        )
                        waiting = replace_state(
                            state,
                            lifecycle_status=LifecycleStatus.WAITING_APPROVAL,
                            pending_approval=pending,
                            pending_interaction=None,
                            pending_tool=None,
                            active_execution_id=plan.execution_id,
                            last_error=None,
                            resume_available=False,
                            revision=state.revision + 1,
                            updated_at=datetime.now(UTC),
                        )
                        approval_event = self._event(
                            ToolApprovalRequired,
                            waiting,
                            request=pending,
                        )
                        waiting_event = self._event(
                            RunWaiting,
                            waiting,
                            waiting_for="approval",
                        )
                        committed = self.store.commit(
                            waiting,
                            state.revision,
                            [approval_event, waiting_event],
                            command,
                            lease_owner=owner,
                            fencing_token=fencing_token,
                            node_execution_update={
                                "execution_id": plan.execution_id,
                                "status": "waiting_approval",
                                "reasoning_summary_json": observer.reasoning_summary_json(),
                            },
                            sdk_run_state={
                                "run_id": state.run_id,
                                "request_id": approval.request_id,
                                "encrypted_state": self.run_state_cipher.encrypt(
                                    approval.state_string
                                ),
                                "sdk_version": "agents-0.22",
                                "agent_definition_version": plan.agent_definition_version,
                            },
                            resolve_sdk_request_id=(
                                resume_for_call.request_id
                                if resume_for_call is not None
                                else None
                            ),
                        )
                        state = waiting
                        for event in committed:
                            yield event
                        break
                    except RunStateCipherError as exc:
                        error = ExecutionFailure(
                            code=exc.code,
                            message="tool approval cannot be durably resumed",
                            retryable=False,
                            auto_retry=False,
                        )
                        next_state, events, terminal = self._handle_node_failure(
                            state,
                            plan,
                            error,
                            config,
                            command,
                            owner,
                            fencing_token,
                            resolve_sdk_request_id=(
                                resume_for_call.request_id
                                if resume_for_call is not None
                                else None
                            ),
                            reasoning_summary_json=observer.reasoning_summary_json(),
                        )
                        state = next_state
                        for event in events:
                            yield event
                        if terminal:
                            break
                        await self._sleep(self._backoff(config, plan.retry_index))
                        continue
                except Exception as exc:
                    error = self._execution_failure(exc)
                    next_state, events, terminal = self._handle_node_failure(
                        state,
                        plan,
                        error,
                        config,
                        command,
                        owner,
                        fencing_token,
                        resolve_sdk_request_id=(
                            resume_for_call.request_id
                            if resume_for_call is not None
                            else None
                        ),
                        reasoning_summary_json=observer.reasoning_summary_json(),
                    )
                    state = next_state
                    for event in events:
                        yield event
                    if terminal:
                        break
                    await self._sleep(self._backoff(config, plan.retry_index))
                    continue
                finally:
                    renewal_stop.set()
                    renewal_task.cancel()
                    await asyncio.gather(renewal_task, return_exceptions=True)

                output_json = json.dumps(
                    sanitize_data(result.model_dump(mode="json")),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                if self.store.is_cancel_requested(state.run_id):
                    canceled, events = self._cancel_at_boundary(
                        state, self.store.cancel_reason(state.run_id)
                    )
                    events.insert(
                        0,
                        self._event(
                            NodeCompleted,
                            canceled,
                            node=plan.node,
                            execution_id=plan.execution_id,
                            next_node=GraphNode.DONE.value,
                        ),
                    )
                    committed = self.store.commit(
                        canceled,
                        state.revision,
                        events,
                        command,
                        lease_owner=owner,
                        fencing_token=fencing_token,
                        node_execution_update={
                            "execution_id": plan.execution_id,
                            "status": "succeeded",
                            "output_json": output_json,
                            "reasoning_summary_json": observer.reasoning_summary_json(),
                            "completed_at": datetime.now(UTC).isoformat(),
                        },
                        clear_cancel=True,
                        resolve_sdk_request_id=(
                            resume_for_call.request_id
                            if resume_for_call is not None
                            else None
                        ),
                    )
                    state = canceled
                    for event in committed:
                        yield event
                    break

                new_state = replace_state(
                    new_state,
                    revision=state.revision + 1,
                    updated_at=datetime.now(UTC),
                    retry_index=0,
                    active_execution_id=None,
                )
                events = [
                    self._event(
                        NodeCompleted,
                        new_state,
                        node=plan.node,
                        execution_id=plan.execution_id,
                        next_node=new_state.current_node.value,
                    )
                ]
                if (
                    new_state.lifecycle_status == LifecycleStatus.WAITING_USER
                    and new_state.pending_interaction is not None
                ):
                    events.extend(
                        [
                            self._event(
                                InputRequired,
                                new_state,
                                request=new_state.pending_interaction,
                            ),
                            self._event(RunWaiting, new_state, waiting_for="user"),
                        ]
                    )
                elif new_state.lifecycle_status == LifecycleStatus.COMPLETED:
                    summary = (
                        new_state.report.executive_summary
                        if new_state.report
                        else "Diagnosis completed"
                    )
                    events.append(
                        self._event(
                            RunCompleted,
                            new_state,
                            outcome=new_state.outcome or DiagnosisOutcome.INCONCLUSIVE,
                            summary=summary,
                        )
                    )
                committed = self.store.commit(
                    new_state,
                    state.revision,
                    events,
                    command,
                    lease_owner=owner,
                    fencing_token=fencing_token,
                    node_execution_update={
                        "execution_id": plan.execution_id,
                        "status": "succeeded",
                        "output_json": output_json,
                        "reasoning_summary_json": observer.reasoning_summary_json(),
                        "completed_at": datetime.now(UTC).isoformat(),
                    },
                    resolve_sdk_request_id=(
                        resume_for_call.request_id
                        if resume_for_call is not None
                        else None
                    ),
                )
                state = new_state
                for event in committed:
                    yield event
                if state.lifecycle_status != LifecycleStatus.RUNNING:
                    break

    def _handle_node_failure(
        self,
        state: DiagnosisState,
        plan,
        error: ExecutionFailure,
        config: ResolvedRunConfig,
        command: AgentCommand,
        owner: str,
        fencing_token: int,
        *,
        resolve_sdk_request_id: str | None = None,
        reasoning_summary_json: str | None = None,
    ) -> tuple[DiagnosisState, list[AgentEvent], bool]:
        exhausted = plan.retry_index >= config.policy.retry.max_retries
        if error.retryable and error.auto_retry and not exhausted:
            next_state = replace_state(
                state,
                active_execution_id=None,
                last_error=None,
                resume_available=False,
                retry_index=plan.retry_index + 1,
                revision=state.revision + 1,
                updated_at=datetime.now(UTC),
            )
            failed_event = self._event(
                NodeAttemptFailed,
                next_state,
                node=plan.node,
                execution_id=plan.execution_id,
                failure=error,
            )
            scheduled = self._event(
                NodeRetryScheduled,
                next_state,
                node=plan.node,
                execution_id=plan.execution_id,
                retry_cycle=plan.retry_cycle,
                retry_index=plan.retry_index + 1,
                delay_seconds=self._backoff(config, plan.retry_index),
                reason=error.code,
            )
            events = self.store.commit(
                next_state,
                state.revision,
                [failed_event, scheduled],
                command,
                lease_owner=owner,
                fencing_token=fencing_token,
                node_execution_update={
                    "execution_id": plan.execution_id,
                    "status": "failed",
                    "error_code": error.code,
                    "error_message": error.message,
                    "retryable": 1,
                    "reasoning_summary_json": reasoning_summary_json,
                    "completed_at": datetime.now(UTC).isoformat(),
                },
                resolve_sdk_request_id=resolve_sdk_request_id,
            )
            return next_state, events, False

        failure = FailureRecord(
            code=error.code,
            message=error.message,
            node=GraphNode(plan.node),
            retryable=error.retryable,
            retry_cycle=plan.retry_cycle,
            retry_index=plan.retry_index,
            resume_available=error.retryable,
        )
        failed = replace_state(
            state,
            lifecycle_status=LifecycleStatus.FAILED,
            last_error=failure,
            resume_available=error.retryable,
            active_execution_id=None,
            revision=state.revision + 1,
            updated_at=datetime.now(UTC),
        )
        events_to_commit: list[AgentEvent] = [
            self._event(
                NodeAttemptFailed,
                failed,
                node=plan.node,
                execution_id=plan.execution_id,
                failure=error,
            )
        ]
        if error.retryable:
            events_to_commit.append(
                self._event(
                    RunResumeAvailable,
                    failed,
                    node=plan.node,
                    retry_cycle=plan.retry_cycle,
                    retry_index=plan.retry_index,
                    reason=error.code,
                )
            )
        events_to_commit.append(self._event(RunFailed, failed, failure=failure))
        events = self.store.commit(
            failed,
            state.revision,
            events_to_commit,
            command,
            lease_owner=owner,
            fencing_token=fencing_token,
            node_execution_update={
                "execution_id": plan.execution_id,
                "status": "failed",
                "error_code": error.code,
                "error_message": error.message,
                "retryable": int(error.retryable),
                "reasoning_summary_json": reasoning_summary_json,
                "completed_at": datetime.now(UTC).isoformat(),
            },
            resolve_sdk_request_id=resolve_sdk_request_id,
        )
        return failed, events, True

    async def _run_cancel(self, command: CancelDiagnosis) -> AsyncIterator[AgentEvent]:
        state = self.store.get_state(command.run_id)
        config = self._config_snapshot(state.config_snapshot_id)
        self._validate_command(command, state)
        reason = str(sanitize_data(command.reason))
        event = self._event(
            RunCancelRequested,
            state,
            reason=reason,
        )
        owner = uuid4().hex
        token: int | None = None
        try:
            try:
                token = self.store.acquire_lease(
                    command.run_id, owner, config.policy.lease_seconds
                )
            except LeaseConflictError:
                yield self.store.request_cancel(
                    command.run_id,
                    state.revision,
                    command,
                    event,
                )
                return
            # A running node owned by this process is not interrupted here.  A
            # control row is enough for the active executor to observe it at its
            # next safe boundary.
            if (
                state.lifecycle_status == LifecycleStatus.RUNNING
                and state.active_execution_id
            ):
                yield self.store.request_cancel(
                    command.run_id,
                    state.revision,
                    command,
                    event,
                )
                return
            canceled, events = self._cancel_at_boundary(state, reason)
            committed = self.store.commit(
                canceled,
                state.revision,
                [event, *events],
                command,
                lease_owner=owner,
                fencing_token=token,
                clear_cancel=True,
                resolve_sdk_request_id=(
                    state.pending_approval.request_id
                    if state.pending_approval is not None
                    else None
                ),
            )
            for committed_event in committed:
                yield committed_event
        finally:
            if token is not None:
                self.store.release_lease(command.run_id, owner, token)

    @staticmethod
    def _cancel_at_boundary(
        state: DiagnosisState, reason: str
    ) -> tuple[DiagnosisState, list[AgentEvent]]:
        reason = str(sanitize_data(reason))
        canceled = replace_state(
            state,
            lifecycle_status=LifecycleStatus.CANCELED,
            current_node=GraphNode.DONE,
            pending_interaction=None,
            pending_tool=None,
            pending_approval=None,
            outcome=None,
            active_execution_id=None,
            revision=state.revision + 1,
            updated_at=datetime.now(UTC),
        )
        return canceled, [
            DiagnosisRuntime._event_static(RunCanceled, canceled, reason=reason)
        ]

    @staticmethod
    def _apply_user_command(
        state: DiagnosisState, command: SubmitUserAnswers | SkipUserInteraction
    ) -> tuple[DiagnosisState, AgentEvent]:
        if isinstance(command, SubmitUserAnswers):
            state = DiagnosisGraph.resume_with_answers(state, command.answers)
            event_type = UserInputSubmitted
            kwargs = {
                "request_id": command.request_id,
                "source_node": state.answers[-1].source_node
                or state.current_node.value,
            }
        else:
            state = DiagnosisGraph.skip_interaction(
                state, command.request_id, command.reason
            )
            skipped = state.skipped_interactions[-1]
            event_type = InputSkipped
            kwargs = {
                "request_id": skipped.request_id,
                "source_node": skipped.source_node,
                "question_ids": skipped.question_ids,
                "reason": skipped.reason,
            }
        new_state = replace_state(
            state,
            revision=state.revision + 1,
            updated_at=datetime.now(UTC),
        )
        return new_state, DiagnosisRuntime._event_static(
            event_type, new_state, **kwargs
        )

    @staticmethod
    def _event_static(event_type: type[AgentEvent], state: DiagnosisState, **kwargs):
        return event_type(
            run_id=state.run_id,
            sequence=1,
            revision=state.revision,
            **kwargs,
        )

    def _event(
        self, event_type: type[AgentEvent], state: DiagnosisState, **kwargs: object
    ) -> AgentEvent:
        sequence = kwargs.pop("sequence", self.store.next_sequence(state.run_id))
        revision = kwargs.pop("revision", state.revision)
        return event_type(
            run_id=state.run_id,
            sequence=sequence,
            revision=revision,
            **kwargs,
        )

    @staticmethod
    def _validate_command(command: AgentCommand, state: DiagnosisState) -> None:
        if (
            command.expected_revision is None
            or command.expected_revision != state.revision
        ):
            raise RevisionConflictError("expected_revision does not match run revision")
        if isinstance(command, (SubmitUserAnswers, SkipUserInteraction)):
            if (
                state.lifecycle_status != LifecycleStatus.WAITING_USER
                or state.pending_interaction is None
            ):
                raise InvalidRunStatusError("run is not waiting for user input")
            if state.pending_interaction.request_id != command.request_id:
                raise PendingRequestMismatchError("pending request_id does not match")
        elif isinstance(command, ResumeDiagnosis):
            if (
                state.lifecycle_status != LifecycleStatus.FAILED
                or not state.resume_available
            ):
                raise InvalidRunStatusError("run cannot be resumed")
        elif isinstance(command, CancelDiagnosis):
            if "cancel" not in state.available_actions:
                raise InvalidRunStatusError("run cannot be cancelled")
        elif isinstance(command, (ApproveTool, RejectTool)):
            if (
                state.lifecycle_status != LifecycleStatus.WAITING_APPROVAL
                or state.pending_approval is None
            ):
                raise InvalidRunStatusError("run is not waiting for tool approval")
            if state.pending_approval.request_id != command.request_id:
                raise PendingRequestMismatchError(
                    "pending approval request_id does not match"
                )

    @staticmethod
    def _approval_decision(
        command: ApproveTool | RejectTool,
    ) -> Literal["approved", "rejected"]:
        return "approved" if isinstance(command, ApproveTool) else "rejected"

    def _approval_decision_matches(self, command: ApproveTool | RejectTool) -> bool:
        record = self.store.get_sdk_run_state(command.run_id, command.request_id)
        return bool(
            record
            and record.get("resolved_at") is None
            and record.get("decision") == self._approval_decision(command)
        )

    def _load_approval_resume(
        self,
        command: ApproveTool | RejectTool,
        *,
        expected_decision: Literal["approved", "rejected"],
    ) -> _ApprovalResume:
        if self.run_state_cipher is None:
            raise ValidationFailedError(
                "SDK approval state is unavailable because BUGLENS_RUN_STATE_KEY is not configured"
            )
        record = self.store.get_sdk_run_state(command.run_id, command.request_id)
        if (
            record is None
            or record.get("resolved_at") is not None
            or record.get("decision") not in {None, expected_decision}
        ):
            raise ValidationFailedError("SDK approval state is unavailable")
        if record.get("decision") is None:
            # The normal command path calls this before the decision is written;
            # the replay path only accepts an already committed matching decision.
            decision = expected_decision
        else:
            decision = str(record["decision"])
        try:
            state_string = self.run_state_cipher.decrypt(
                str(record.get("encrypted_state") or "")
            )
        except RunStateCipherError as exc:
            raise ValidationFailedError(
                "SDK approval state cannot be decrypted"
            ) from exc
        state = self.store.get_state(command.run_id)
        tool_call_ids = (
            state.pending_approval.tool_call_ids
            if state.pending_approval is not None
            and state.pending_approval.request_id == command.request_id
            else []
        )
        return _ApprovalResume(
            request_id=command.request_id,
            state_string=state_string,
            decision=decision,  # type: ignore[arg-type]
            tool_call_ids=list(tool_call_ids),
        )

    def _check_resume_compatibility(self, state: DiagnosisState) -> None:
        session_id_for_state = getattr(self.graph, "session_id_for_state", None)
        session_id = (
            session_id_for_state(state)
            if callable(session_id_for_state)
            else f"{state.run_id}:{state.current_node.value}"
        )
        checker = getattr(self.graph.runner, "session_available", None)
        if callable(checker) and not checker(session_id):
            raise ValidationFailedError("SDK session is unavailable for Resume")
        executions = self.store.list_node_executions(
            run_id=state.run_id,
            node=state.current_node.value,
            limit=1_000,
        )
        if executions:
            snapshot = self._config_snapshot(state.config_snapshot_id)
            expected_version = getattr(
                self.graph, "agent_definition_version_for_state", None
            )
            expected = (
                expected_version(state, snapshot)
                if callable(expected_version)
                else (
                    f"{snapshot.policy.node(state.current_node.value).prompt_version}"
                    ":agents-0.22"
                )
            )
            recorded = executions[-1].get("agent_definition_version")
            if recorded not in {None, "unknown", expected}:
                raise ValidationFailedError(
                    "agent definition is incompatible with the saved execution"
                )

    @staticmethod
    def _execution_failure(exc: Exception) -> ExecutionFailure:
        if isinstance(exc, NodeExecutionError):
            return ExecutionFailure(
                code=exc.code,
                message=str(exc),
                retryable=exc.retryable,
                auto_retry=exc.auto_retry,
            )
        if isinstance(exc, (ValidationError, ValueError)):
            return ExecutionFailure(
                code="graph_contract_error",
                message="Node output violated the diagnosis contract",
                retryable=False,
                auto_retry=False,
            )
        return ExecutionFailure(
            code="node_execution_failed",
            message="Agent execution failed",
            retryable=True,
            auto_retry=True,
        )

    @classmethod
    def _failure_from_exception(
        cls, exc: Exception, state: DiagnosisState
    ) -> FailureRecord:
        error = cls._execution_failure(exc)
        return FailureRecord(
            code=error.code,
            message=error.message,
            node=state.current_node if state.current_node != GraphNode.DONE else None,
            retryable=error.retryable,
            retry_cycle=state.retry_cycle,
            retry_index=state.retry_index,
            resume_available=error.retryable,
        )

    @staticmethod
    def _backoff(config: ResolvedRunConfig, retry_index: int) -> float:
        policy = config.policy.retry
        return min(
            policy.max_delay_seconds,
            policy.initial_delay_seconds * (policy.multiplier**retry_index),
        )

    async def _renew_lease_during_call(
        self,
        run_id: str,
        owner: str,
        fencing_token: int,
        config: ResolvedRunConfig,
        stop: asyncio.Event,
        failures: list[Exception],
    ) -> None:
        """Renew a command lease without holding a SQLite transaction open."""
        interval = config.policy.lease_renewal_seconds
        try:
            while True:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=interval)
                    return
                except TimeoutError:
                    pass
                if stop.is_set():
                    return
                try:
                    self.store.renew_lease(
                        run_id,
                        owner,
                        fencing_token,
                        config.policy.lease_seconds,
                    )
                except FencingTokenError as exc:
                    failures.append(exc)
                    return
                except Exception:
                    failures.append(FencingTokenError("lease renewal failed"))
                    return
        except asyncio.CancelledError:
            return

    async def get_run(self, run_id: str) -> RunView:
        state = self.store.get_state(run_id)
        config = self._config_snapshot(state.config_snapshot_id)
        state = self._recover_stale_run(state, config)
        state = self._hydrate_evidence(state)
        return RunView.model_validate(state.model_dump())

    async def events(self, run_id: str, after: int = 0) -> AsyncIterator[AgentEvent]:
        self.store.get_state(run_id)
        for event in self.store.events_after(run_id, after):
            yield event

    def _recover_stale_run(
        self, state: DiagnosisState, config: ResolvedRunConfig
    ) -> DiagnosisState:
        """Turn an orphaned in-flight node into an explicit resumable failure.

        A read or a subsequent command can be the first request after a process
        crash.  Recovery briefly acquires the expired lease, marks the
        execution audit rows, and commits the same failure state/events that a
        live executor would have produced.  If another executor still owns a
        lease, it is left entirely alone.
        """
        if (
            state.lifecycle_status != LifecycleStatus.RUNNING
            or not state.active_execution_id
            or self.store.lease_active(state.run_id)
            or self.store.has_unresolved_sdk_decision(state.run_id)
        ):
            return state

        owner = f"recovery-{uuid4().hex}"
        token: int | None = None
        try:
            token = self.store.acquire_lease(
                state.run_id, owner, config.policy.lease_seconds
            )
            current = self.store.get_state(state.run_id)
            if (
                current.lifecycle_status != LifecycleStatus.RUNNING
                or not current.active_execution_id
            ):
                return current
            interrupted = self.store.recover_interrupted_executions(state.run_id)
            if not interrupted:
                return current
            failure = ExecutionFailure(
                code="process_interrupted",
                message="node execution was interrupted before completion",
                retryable=True,
            )
            failure_record = FailureRecord(
                code=failure.code,
                message=failure.message,
                node=current.current_node,
                retryable=True,
                retry_cycle=current.retry_cycle,
                retry_index=current.retry_index,
                resume_available=True,
            )
            recovered = replace_state(
                current,
                lifecycle_status=LifecycleStatus.FAILED,
                last_error=failure_record,
                resume_available=True,
                active_execution_id=None,
                revision=current.revision + 1,
                updated_at=datetime.now(UTC),
            )
            events: list[AgentEvent] = []
            for row in interrupted:
                try:
                    node = GraphNode(str(row["node"]))
                except ValueError:
                    node = current.current_node
                events.append(
                    self._event(
                        NodeAttemptFailed,
                        recovered,
                        node=node.value,
                        execution_id=str(row["execution_id"]),
                        failure=failure,
                    )
                )
            events.extend(
                [
                    self._event(
                        RunResumeAvailable,
                        recovered,
                        node=current.current_node.value,
                        retry_cycle=current.retry_cycle,
                        retry_index=current.retry_index,
                        reason=failure.code,
                    ),
                    self._event(RunFailed, recovered, failure=failure_record),
                ]
            )
            self.store.commit(
                recovered,
                current.revision,
                events,
                lease_owner=owner,
                fencing_token=token,
            )
            return recovered
        except LeaseConflictError:
            return self.store.get_state(state.run_id)
        finally:
            if token is not None:
                self.store.release_lease(state.run_id, owner, token)

    def _hydrate_evidence(self, state: DiagnosisState) -> DiagnosisState:
        """Expose registered tool evidence to later node contexts and readers."""
        known = {record.evidence_id for record in state.source_evidence}
        additional = [
            record
            for record in self.store.list_evidence(state.run_id)
            if record.evidence_id not in known
        ]
        if not additional:
            return state
        return replace_state(
            state,
            source_evidence=[*state.source_evidence, *additional][:100],
        )

    def _config_snapshot(self, snapshot_id: str) -> ResolvedRunConfig:
        config = self._config_cache.get(snapshot_id)
        if config is None:
            config = self.store.get_config_snapshot(snapshot_id)
            self._config_cache[snapshot_id] = config
        return config

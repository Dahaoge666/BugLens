"""Shared short-lived Agent Runtime used by local and remote adapters."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import AsyncIterator, Protocol
from uuid import uuid4

from agents import trace

from .agents import NodeExecutionError
from .config import ConfigRepository, ResolvedRunConfig
from .graph import DiagnosisGraph
from .infra import (
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
    FailureRecord,
    GraphNode,
    LifecycleStatus,
    UserAnswer,
)
from .protocol.commands import (
    AgentCommand,
    CancelDiagnosis,
    StartDiagnosis,
    SubmitUserAnswers,
)
from .protocol.events import (
    AgentEvent,
    InputRequired,
    NodeCompleted,
    NodeStarted,
    RunCanceled,
    RunCompleted,
    RunFailed,
    RunStarted,
    RunWaiting,
)
from .security import sanitize_data


class RunView(DiagnosisState):
    """Read model returned to adapters; it contains no SDK session details."""


class AgentRuntime(Protocol):
    def run(self, command: AgentCommand) -> AsyncIterator[AgentEvent]: ...

    async def get_run(self, run_id: str) -> RunView: ...


class DiagnosisRuntime:
    def __init__(
        self,
        graph: DiagnosisGraph,
        store: SQLiteCheckpointStore,
        configs: ConfigRepository,
        *,
        tracing_enabled: bool = True,
    ) -> None:
        self.graph = graph
        self.store = store
        self.configs = configs
        self.tracing_enabled = tracing_enabled
        self.owner = uuid4().hex

    async def run(
        self,
        command: AgentCommand,
        resolved_config: ResolvedRunConfig | None = None,
    ) -> AsyncIterator[AgentEvent]:
        previous = self.store.get_command(command.command_id)
        if previous is not None:
            for event in previous:
                yield event
            return

        config = resolved_config
        if isinstance(command, StartDiagnosis):
            if command.expected_revision is not None:
                raise ValidationFailedError(
                    "start command cannot set expected_revision"
                )
            config = config or self.configs.resolve(command.profile)
            self.store.save_config_snapshot(config)
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
            state.run_id = command.run_id
            state.lifecycle_status = LifecycleStatus.RUNNING
            started = self._event(
                RunStarted,
                state,
                profile=config.profile,
                config_snapshot_id=config.snapshot_id,
                config_version=config.config_version,
            )
            self.store.create_run(state, [started], command)
            yield started
        else:
            state = self.store.get_state(command.run_id)
            config = self.store.get_config_snapshot(state.config_snapshot_id)
            self._validate_command(command, state)

        assert config is not None
        self.store.acquire_lease(command.run_id, self.owner)
        try:
            if isinstance(command, CancelDiagnosis):
                yield await self._cancel(state, command)
                return
            if isinstance(command, SubmitUserAnswers):
                self._apply_answers(state, command)
            async for event in self._drive(
                state,
                config,
                command if not isinstance(command, StartDiagnosis) else None,
            ):
                yield event
        except (
            RunNotFoundError,
            RevisionConflictError,
            InvalidRunStatusError,
            PendingRequestMismatchError,
            ValidationFailedError,
            LeaseConflictError,
        ):
            raise
        except Exception as exc:
            logging.exception(
                "Runtime execution failed for run %s at node %s",
                getattr(state, "run_id", "?"),
                getattr(state, "current_node", None),
            )
            failure = FailureRecord(
                code="node_execution_failed"
                if isinstance(exc, NodeExecutionError)
                else "runtime_failed",
                message="Agent execution failed"
                if isinstance(exc, NodeExecutionError)
                else "Runtime execution failed",
                node=state.current_node
                if state.current_node != GraphNode.DONE
                else None,
                retryable=isinstance(exc, NodeExecutionError),
            )
            state.last_error = failure
            state.lifecycle_status = LifecycleStatus.FAILED
            state.current_node = GraphNode.DONE
            old_revision = state.revision
            state.revision += 1
            state.updated_at = datetime.now(UTC)
            event = self._event(RunFailed, state, failure=failure)
            self.store.commit(
                state,
                old_revision,
                [event],
                None if isinstance(command, StartDiagnosis) else command,
            )
            yield event
        finally:
            self.store.release_lease(command.run_id, self.owner)

    async def _drive(
        self,
        state: DiagnosisState,
        config: ResolvedRunConfig,
        command: AgentCommand | None,
    ) -> AsyncIterator[AgentEvent]:
        first = True
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
                node = GraphNode(state.current_node)
                if node == GraphNode.DONE:
                    break
                old_revision = state.revision
                state.lifecycle_status = LifecycleStatus.RUNNING
                try:
                    node_completed = await self.graph.step(state, config)
                except Exception:
                    state.revision = old_revision
                    raise
                state.revision = old_revision + 1
                state.updated_at = datetime.now(UTC)
                sequence = self._next_sequence(state.run_id)
                events: list[AgentEvent] = [
                    self._event(
                        NodeStarted,
                        state,
                        node=node.value,
                        revision=old_revision,
                        sequence=sequence,
                    ),
                    self._event(
                        NodeCompleted,
                        state,
                        node=node_completed,
                        next_node=state.current_node.value,
                        sequence=sequence + 1,
                    ),
                ]
                if (
                    state.lifecycle_status == LifecycleStatus.WAITING_USER
                    and state.pending_interaction
                ):
                    events.extend(
                        [
                            self._event(
                                InputRequired,
                                state,
                                request=state.pending_interaction,
                                sequence=sequence + 2,
                            ),
                            self._event(
                                RunWaiting,
                                state,
                                waiting_for="user",
                                sequence=sequence + 3,
                            ),
                        ]
                    )
                elif state.current_node == GraphNode.DONE:
                    state.lifecycle_status = LifecycleStatus.COMPLETED
                    state.outcome = state.outcome or DiagnosisOutcome.INCONCLUSIVE
                    summary = (
                        state.report.executive_summary
                        if state.report
                        else "Diagnosis completed"
                    )
                    events.append(
                        self._event(
                            RunCompleted,
                            state,
                            outcome=state.outcome,
                            summary=summary,
                            sequence=sequence + 2,
                        )
                    )
                self.store.commit(
                    state, old_revision, events, command if first else None
                )
                first = False
                for event in events:
                    yield event
                if state.lifecycle_status != LifecycleStatus.RUNNING:
                    break

    async def _cancel(
        self, state: DiagnosisState, command: CancelDiagnosis
    ) -> AgentEvent:
        if state.lifecycle_status in {
            LifecycleStatus.COMPLETED,
            LifecycleStatus.FAILED,
            LifecycleStatus.CANCELED,
        }:
            raise InvalidRunStatusError("run is already terminal")
        state.lifecycle_status = LifecycleStatus.CANCELED
        state.current_node = GraphNode.DONE
        state.pending_interaction = None
        state.pending_tool = None
        state.pending_approval = None
        old_revision = state.revision
        state.revision += 1
        state.updated_at = datetime.now(UTC)
        event = self._event(RunCanceled, state, reason=command.reason)
        self.store.commit(state, old_revision, [event], command)
        return event

    @staticmethod
    def _apply_answers(state: DiagnosisState, command: SubmitUserAnswers) -> None:
        request = state.pending_interaction
        if state.lifecycle_status != LifecycleStatus.WAITING_USER or request is None:
            raise InvalidRunStatusError("run is not waiting for user input")
        if request.request_id != command.request_id:
            raise PendingRequestMismatchError("pending request_id does not match")
        request.validate_answers(command.answers)
        clean = [
            UserAnswer.model_validate(sanitize_data(answer.model_dump()))
            for answer in command.answers
        ]
        state.answers.extend(clean)
        state.clarification_round += 1
        state.next_node_input = {"answers": [answer.model_dump() for answer in clean]}
        state.current_node = GraphNode(request.resume_node)
        state.pending_interaction = None
        state.lifecycle_status = LifecycleStatus.RUNNING

    @staticmethod
    def _validate_command(command: AgentCommand, state: DiagnosisState) -> None:
        if command.expected_revision != state.revision:
            raise RevisionConflictError("expected_revision does not match run revision")

    def _event(
        self, event_type: type[AgentEvent], state: DiagnosisState, **kwargs: object
    ) -> AgentEvent:
        sequence = kwargs.pop("sequence", self._next_sequence(state.run_id))
        revision = kwargs.pop("revision", state.revision)
        return event_type(
            run_id=state.run_id,
            sequence=sequence,
            revision=revision,
            **kwargs,
        )

    def _next_sequence(self, run_id: str) -> int:
        events = self.store.events_after(run_id)
        return (events[-1].sequence if events else 0) + 1

    async def get_run(self, run_id: str) -> RunView:
        return RunView.model_validate(self.store.get_state(run_id).model_dump())

    async def events(self, run_id: str, after: int = 0) -> AsyncIterator[AgentEvent]:
        # Validate the run before yielding an empty history. Otherwise a typo in
        # the run id would look identical to a valid request past the latest
        # sequence number.
        self.store.get_state(run_id)
        for event in self.store.events_after(run_id, after):
            yield event

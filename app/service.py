"""Application operations shared by HTTP and other future entry points."""

from collections.abc import Awaitable, Callable

from .agents import NodeExecutionError
from .graph import DiagnosisGraph
from .models import (
    CreateDiagnosisRequest,
    DiagnosisReport,
    DiagnosisState,
    SubmitAnswersRequest,
    UserAnswer,
)


class DiagnosisFailed(RuntimeError):
    def __init__(self, run_id: str) -> None:
        super().__init__("Diagnosis node failed")
        self.run_id = run_id


class DiagnosisService:
    """Load runs and persist controlled failures, independently of HTTP."""

    def __init__(self, graph: DiagnosisGraph) -> None:
        self.graph = graph

    def get(self, run_id: str) -> DiagnosisState:
        return self.graph.store.load(run_id)

    async def create(self, request: CreateDiagnosisRequest) -> DiagnosisState:
        state = DiagnosisState.create(
            request.question,
            request.context,
            request.evidence,
            request.max_attempts,
            request.max_clarification_rounds,
        )
        return await self._execute(state, self.graph.start)

    async def answer(
        self, run_id: str, request: SubmitAnswersRequest
    ) -> DiagnosisState:
        state = self.get(run_id)
        answers = [
            UserAnswer(request_id=request.request_id, **item.model_dump())
            for item in request.answers
        ]
        return await self._execute(
            state, lambda state: self.graph.resume(state, answers)
        )

    async def cancel(self, run_id: str) -> DiagnosisState:
        return await self._execute(self.get(run_id), self.graph.cancel)

    async def _execute(
        self,
        state: DiagnosisState,
        operation: Callable[[DiagnosisState], Awaitable[DiagnosisState]],
    ) -> DiagnosisState:
        try:
            return await operation(state)
        except NodeExecutionError as exc:
            state.status = "inconclusive"
            state.pending_interaction = None
            state.report = DiagnosisReport(
                executive_summary="定位节点未能完成运行。",
                status_explanation="尚未确认根因。",
                next_actions=["检查模型服务配置后创建新的定位任务。"],
            )
            self.graph.store.save(state)
            raise DiagnosisFailed(state.run_id) from exc

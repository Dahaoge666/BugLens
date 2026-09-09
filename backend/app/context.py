"""Deterministic assembly of the business context visible to each node."""

from __future__ import annotations

from collections.abc import Iterable

from .models import (
    AnalyzeInput,
    DiagnosisContext,
    DiagnosisState,
    EvaluationInput,
    EvidenceRecord,
    GraphNode,
    InvestigationInput,
    ResolvedTarget,
    SummaryInput,
    UserAnswer,
)


class ContextAssembler:
    """Build bounded node inputs without reading SDK history or external state."""

    def context(self, state: DiagnosisState) -> DiagnosisContext:
        return state.context

    def evidence(self, state: DiagnosisState) -> list[EvidenceRecord]:
        records: list[EvidenceRecord] = []
        seen: set[str] = set()

        def add(items: Iterable[EvidenceRecord]) -> None:
            for item in items:
                if item.evidence_id in seen:
                    continue
                records.append(item)
                seen.add(item.evidence_id)

        add(state.source_evidence)
        if state.analysis is not None:
            add(state.analysis.extracted_evidence)
        for answer in state.answers:
            add(answer.attachments)
        return records[:100]

    def answers(self, state: DiagnosisState, node: str) -> list[UserAnswer]:
        source_nodes = {node}
        # Evaluate clarification is deliberately routed back to Investigate;
        # preserve that answer in the next investigation input as well.
        if node == GraphNode.INVESTIGATE.value:
            source_nodes.add(GraphNode.EVALUATE.value)
        selected = [
            answer
            for answer in state.answers
            if answer.source_node is None or answer.source_node in source_nodes
        ]
        return selected[-20:]

    @staticmethod
    def target(state: DiagnosisState) -> ResolvedTarget | None:
        if not state.environment_snapshot_id or not state.target.environment_id:
            return None
        return ResolvedTarget(
            environment_id=state.target.environment_id,
            primary_service_id=state.target.primary_service_id,
        )

    def build(self, node: GraphNode, state: DiagnosisState):
        evidence = self.evidence(state)
        target = self.target(state)
        if node == GraphNode.ANALYZE:
            return AnalyzeInput(
                question=state.user_question,
                context=self.context(state),
                evidence=evidence,
                target=target,
                environment_hint=(
                    target.environment_id
                    if target is not None
                    else self.context(state).environment
                ),
                clarification_answers=self.answers(state, "analyze"),
            )
        if node == GraphNode.INVESTIGATE:
            if state.analysis is None:
                raise ValueError("investigation context requires analysis")
            return InvestigationInput(
                analysis=state.analysis,
                context=self.context(state),
                evidence=evidence,
                target=target,
                previous_evaluation=state.evaluation,
                previous_investigation=state.investigation,
                clarification_answers=self.answers(state, "investigate"),
                prompt_config_version=state.config_version,
                investigation_attempt=max(1, state.attempt),
            )
        if node == GraphNode.EVALUATE:
            if state.analysis is None or state.investigation is None:
                raise ValueError(
                    "evaluation context requires analysis and investigation"
                )
            return EvaluationInput(
                analysis=state.analysis,
                investigation=state.investigation,
                context=self.context(state),
                evidence=evidence,
                target=target,
                previous_evaluation=state.evaluation,
                progress_history=state.progress_deltas[-20:],
                rubric_version=state.config_version,
            )
        if node == GraphNode.SUMMARIZE:
            if state.analysis is None:
                raise ValueError("summary context requires analysis")
            status = (
                "inconclusive"
                if state.outcome and state.outcome.value == "inconclusive"
                else "completed"
            )
            limitations = list(state.analysis.missing_information)
            if state.investigation is not None:
                limitations.extend(state.investigation.limitations)
            return SummaryInput(
                analysis=state.analysis,
                investigation=state.investigation,
                evaluation=state.evaluation,
                context=self.context(state),
                evidence=evidence,
                target=target,
                skipped_interactions=state.skipped_interactions,
                status=status,
                attempts=state.attempt,
                limitations=limitations[:50],
            )
        raise ValueError(f"cannot assemble input for {node.value}")


__all__ = ["ContextAssembler"]

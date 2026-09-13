"""Deterministic case extraction and bounded, scoped historical guidance."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable

from .context import ContextAssembler
from .models import (
    DiagnosisMemory,
    DiagnosisOutcome,
    DiagnosisState,
    LifecycleStatus,
    MemoryHypothesis,
    MemoryMatch,
    ProblemCategory,
)
from .security import sanitize_text


def memory_scope(state: DiagnosisState) -> tuple[str, str, str]:
    """Missing scope values form their own scope, never a wildcard."""
    tenant = state.context.attributes.get("tenant_id")
    return (
        tenant if isinstance(tenant, str) else "",
        state.target.environment_id or state.context.environment or "",
        state.target.primary_service_id or state.context.service or "",
    )


def _texts(values: Iterable[str], limit: int = 10) -> list[str]:
    return list(dict.fromkeys(_bounded_text(v, 500) for v in values if v.strip()))[
        :limit
    ]


def _bounded_text(value: str, limit: int) -> str:
    text = sanitize_text(value)[:limit]
    # A partial redaction marker would expand on checkpoint sanitization and
    # could exceed the field bound. Keep truncation stable across resumes.
    start = text.rfind("[")
    if start >= 0 and any(
        marker.startswith(text[start:]) and text[start:] != marker
        for marker in ("[REDACTED]", "[EMAIL REDACTED]", "[PHONE REDACTED]")
    ):
        text = text[:start]
    return text


def _optional_text(value: str | None, limit: int) -> str | None:
    return _bounded_text(value, limit) if value is not None else None


def build_memory(state: DiagnosisState) -> DiagnosisMemory | None:
    if (
        state.lifecycle_status != LifecycleStatus.COMPLETED
        or state.outcome is None
        or state.analysis is None
        or state.report is None
    ):
        return None
    investigation = state.investigation
    known_ids = {item.evidence_id for item in ContextAssembler().evidence(state)}
    hypotheses = []
    conclusion = None
    for hypothesis in investigation.hypotheses if investigation else []:
        supported = (
            state.outcome == DiagnosisOutcome.CONFIRMED
            and state.evaluation is not None
            and state.evaluation.passed
            and state.report.primary_conclusion == hypothesis.cause
            and investigation.primary_conclusion == hypothesis.cause
            and bool(hypothesis.supporting_evidence)
            and set(hypothesis.supporting_evidence).issubset(known_ids)
        )
        if supported:
            conclusion = _bounded_text(hypothesis.cause, 1_000)
        hypotheses.append(
            MemoryHypothesis(
                cause=_bounded_text(hypothesis.cause, 1_000),
                status="evidence_supported" if supported else "unverified",
                rationale=_bounded_text(hypothesis.rationale, 1_000),
                historical_evidence_ids=[
                    _bounded_text(item, 128)
                    for item in hypothesis.supporting_evidence
                    if item in known_ids
                ],
                verification_steps=_texts(hypothesis.verification_steps, 5),
            )
        )
    _, environment, service = memory_scope(state)
    gaps = [*state.analysis.missing_information]
    limitations = [*state.report.limitations]
    if investigation:
        gaps.extend(investigation.evidence_gaps)
        limitations.extend(investigation.limitations)
    if state.evaluation:
        limitations.extend(state.evaluation.deficiencies)
    limitations.extend(
        f"用户跳过补充信息：{item.reason}" for item in state.skipped_interactions
    )
    case = DiagnosisMemory(
        memory_id="mem_" + hashlib.sha256(state.run_id.encode()).hexdigest()[:24],
        source_run_id=_bounded_text(state.run_id, 128),
        source_revision=state.revision,
        source_config_version=_bounded_text(state.config_version, 128),
        recorded_at=state.updated_at,
        category=state.analysis.category,
        outcome=state.outcome,
        problem_summary=_bounded_text(state.analysis.summary, 2_000),
        symptoms=_texts(state.analysis.symptoms),
        environment=_optional_text(environment or None, 256),
        service=_optional_text(service or None, 256),
        component=_optional_text(state.context.component, 256),
        version=_optional_text(state.context.version, 256),
        runtime=_optional_text(state.context.runtime, 256),
        confirmed_conclusion=conclusion,
        hypotheses=hypotheses,
        information_gaps=_texts(gaps),
        next_data_to_collect=_texts(
            [
                *(investigation.next_data_to_collect if investigation else []),
                *state.report.next_actions,
            ]
        ),
        limitations=_texts(limitations),
    )
    return case


def search_terms(value: str) -> set[str]:
    """Keep technical words/error codes and Chinese bigrams without dependencies."""
    value = re.sub(r"\[[^\]]*REDACTED[^\]]*\]", " ", sanitize_text(value))
    terms = {
        term
        for term in re.findall(r"[a-z0-9_][a-z0-9_.:-]+", value.casefold())
        if len(term) <= 128
    }
    terms.difference_update({"the", "and", "for", "with", "from", "this", "that"})
    for phrase in re.findall(r"[\u3400-\u9fff]+", value):
        terms.update(phrase[index : index + 2] for index in range(len(phrase) - 1))
    return terms


def rank_memories(
    state: DiagnosisState, cases: Iterable[DiagnosisMemory], *, limit: int = 3
) -> list[MemoryMatch]:
    query = [state.user_question, state.context.actual_behavior or ""]
    if state.analysis:
        query.extend([state.analysis.summary, *state.analysis.symptoms])
    query.extend(item.content for item in ContextAssembler().evidence(state))
    query_terms = search_terms(" ".join(query))
    matches = []
    _, environment, service = memory_scope(state)
    for case in cases:
        if (
            case.source_run_id == state.run_id
            or (case.environment or "") != environment
            or (case.service or "") != service
        ):
            continue
        if (
            state.analysis
            and state.analysis.category != ProblemCategory.UNKNOWN
            and case.category != state.analysis.category
        ):
            continue
        terms = search_terms(" ".join([case.problem_summary, *case.symptoms]))
        common = terms & query_terms
        if not common:
            continue
        similarity = len(common) / len(terms | query_terms)
        # Category/scope alone cannot make an unrelated case a match.
        if similarity < 0.08:
            continue
        matches.append(
            MemoryMatch(
                case=case,
                similarity=round(similarity, 4),
                matched_terms=sorted(common)[:10],
            )
        )
    matches.sort(
        key=lambda item: (item.similarity, item.case.recorded_at, item.case.memory_id),
        reverse=True,
    )
    selected = []
    used_bytes = 2
    for match in matches:
        size = len(match.model_dump_json().encode("utf-8")) + 1
        if used_bytes + size > 32_768:
            continue
        if len(selected) >= max(0, min(limit, 3)):
            break
        selected.append(match)
        used_bytes += size
    return selected

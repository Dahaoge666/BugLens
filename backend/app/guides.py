"""Classified diagnosis guides and a model-free question-entry lookup."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal, Protocol
from uuid import uuid4

from pydantic import Field, model_validator

from .memory import _bounded_text, memory_scope, search_terms
from .models import (
    ContextValue,
    DiagnosisMemory,
    DiagnosisOutcome,
    DiagnosisState,
    EvidenceRecord,
    ProblemCategory,
    StrictModel,
    TargetSpec,
)
from .security import sanitize_data

GuideText = Annotated[str, Field(min_length=1, max_length=500)]

CATEGORY_LABELS = {
    ProblemCategory.APPLICATION_ERROR: "应用错误",
    ProblemCategory.PERFORMANCE: "性能问题",
    ProblemCategory.AVAILABILITY: "可用性问题",
    ProblemCategory.DATA_CONSISTENCY: "数据一致性",
    ProblemCategory.CONFIGURATION: "配置问题",
    ProblemCategory.INTEGRATION: "集成问题",
    ProblemCategory.SECURITY_ACCESS: "安全与访问",
}


class GuideNotFoundError(ValueError):
    code = "guide_not_found"


class GuideRevisionConflictError(ValueError):
    code = "guide_revision_conflict"


class GuideStep(StrictModel):
    instruction: str = Field(min_length=1, max_length=1_000)
    expected_observation: str | None = Field(default=None, max_length=1_000)


class GuideContent(StrictModel):
    title: str = Field(min_length=1, max_length=200)
    category: ProblemCategory
    phenomenon: str = Field(min_length=1, max_length=2_000)
    symptoms: list[GuideText] = Field(min_length=1, max_length=10)
    environment: str | None = Field(default=None, min_length=1, max_length=256)
    service: str | None = Field(default=None, min_length=1, max_length=256)
    component: str | None = Field(default=None, max_length=256)
    version: str | None = Field(default=None, max_length=256)
    runtime: str | None = Field(default=None, max_length=256)
    applicability: str = Field(
        default="核对本次现象与适用条件，再按步骤采集证据。", max_length=2_000
    )
    steps: list[GuideStep] = Field(min_length=1, max_length=10)
    historical_conclusion: str | None = Field(default=None, max_length=1_000)
    unverified_causes: list[GuideText] = Field(default_factory=list, max_length=3)
    limitations: list[GuideText] = Field(default_factory=list, max_length=10)
    enabled: bool = True

    @model_validator(mode="after")
    def require_specific_phenomenon(self):
        if self.category == ProblemCategory.UNKNOWN:
            raise ValueError("定位指南必须指定问题分类")
        if not self.title.strip() or not self.phenomenon.strip():
            raise ValueError("指南标题与问题现象不能为空")
        if any(not item.strip() for item in self.symptoms):
            raise ValueError("指南症状不能为空")
        if any(not step.instruction.strip() for step in self.steps):
            raise ValueError("指南定位步骤不能为空")
        return self


class DiagnosisGuide(GuideContent):
    guide_id: str = Field(min_length=1, max_length=128)
    revision: int = Field(default=1, ge=1)
    origin: Literal["memory", "manual"]
    source_memory_id: str | None = Field(default=None, max_length=128)
    source_run_id: str | None = Field(default=None, max_length=128)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class GuideImportRequest(StrictModel):
    tenant_id: str | None = Field(default=None, max_length=128)
    guides: list[GuideContent] = Field(min_length=1, max_length=50)


class GuideUpdateRequest(StrictModel):
    expected_revision: int = Field(ge=1)
    guide: GuideContent


class GuideQuery(StrictModel):
    question: str = Field(min_length=1, max_length=12_000)
    context: dict[str, ContextValue] = Field(default_factory=dict)
    evidence: list[EvidenceRecord] = Field(default_factory=list, max_length=100)
    target: TargetSpec = Field(default_factory=TargetSpec)
    category: ProblemCategory | None = None


class GuideMatch(StrictModel):
    guide: DiagnosisGuide
    similarity: float = Field(ge=0, le=1)
    matched_terms: list[Annotated[str, Field(max_length=128)]] = Field(max_length=10)


class GuideLookup(StrictModel):
    next_action: Literal["confirm_similarity", "diagnose"]
    matches: list[GuideMatch] = Field(default_factory=list, max_length=3)


class GuideList(StrictModel):
    items: list[DiagnosisGuide]
    total: int = Field(ge=0)


class GuideStore(Protocol):
    def guide_candidates(
        self, state: DiagnosisState, category: ProblemCategory | None
    ) -> list[DiagnosisGuide]: ...
    def import_guides(self, guides: list[DiagnosisGuide], tenant_id: str) -> None: ...
    def update_guide(
        self, guide_id: str, request: GuideUpdateRequest
    ) -> DiagnosisGuide: ...
    def list_guides(
        self, *, category: str | None, limit: int, offset: int
    ) -> GuideList: ...


def guide_from_memory(case: DiagnosisMemory) -> DiagnosisGuide | None:
    """Only turn specific, evidence-supported cases into entry-point guides."""
    if (
        case.category == ProblemCategory.UNKNOWN
        or case.outcome != DiagnosisOutcome.CONFIRMED
        or not case.problem_summary.strip()
        or not case.symptoms
        or not case.confirmed_conclusion
    ):
        return None
    supported = [
        item
        for item in case.hypotheses
        if item.status == "evidence_supported"
        and item.cause == case.confirmed_conclusion
        and item.historical_evidence_ids
    ]
    if not supported:
        return None
    instructions = list(
        dict.fromkeys(
            step
            for hypothesis in [
                *supported,
                *(item for item in case.hypotheses if item not in supported),
            ]
            for step in hypothesis.verification_steps
            if step.strip()
        )
    )[:10]
    if not instructions:
        return None
    return DiagnosisGuide(
        guide_id="guide_" + case.memory_id.removeprefix("mem_"),
        title=_bounded_text(case.problem_summary.strip(), 200),
        category=case.category,
        phenomenon=case.problem_summary,
        symptoms=case.symptoms,
        environment=case.environment,
        service=case.service,
        component=case.component,
        version=case.version,
        runtime=case.runtime,
        applicability="来源诊断中的现象与适用环境。历史结论只在来源诊断内成立，需核对本次版本与组件。",
        steps=[GuideStep(instruction=step) for step in instructions],
        historical_conclusion=case.confirmed_conclusion,
        unverified_causes=[
            _bounded_text(item.cause, 500)
            for item in case.hypotheses
            if item.status == "unverified"
        ],
        limitations=list(dict.fromkeys([*case.limitations, *case.information_gaps]))[
            :10
        ],
        origin="memory",
        source_memory_id=case.memory_id,
        source_run_id=case.source_run_id,
        created_at=case.recorded_at,
        updated_at=case.recorded_at,
    )


class GuideApplicationService:
    def __init__(self, store: GuideStore):
        self.store = store

    def lookup(
        self,
        query: GuideQuery,
        *,
        tenant_id: str | None = None,
        bind_identity: bool = False,
    ) -> GuideLookup:
        context = dict(query.context)
        if bind_identity:
            context.pop("tenant_id", None)
            if tenant_id:
                context["tenant_id"] = tenant_id
        state = DiagnosisState.create(
            query.question, context, query.evidence, target=query.target
        )
        query_terms = search_terms(
            " ".join(
                [
                    state.user_question,
                    state.context.actual_behavior or "",
                    *(item.content for item in state.source_evidence),
                ]
            )
        )
        matches = []
        _, environment, service = memory_scope(state)
        for guide in self.store.guide_candidates(state, query.category):
            if not guide.enabled:
                continue
            if guide.origin == "memory":
                if (guide.environment or "") != environment or (
                    guide.service or ""
                ) != service:
                    continue
            elif (guide.environment and guide.environment != environment) or (
                guide.service and guide.service != service
            ):
                continue
            if any(
                getattr(guide, key)
                and getattr(state.context, key)
                and getattr(guide, key) != getattr(state.context, key)
                for key in ("component", "version", "runtime")
            ):
                continue
            terms = search_terms(
                " ".join([guide.title, guide.phenomenon, *guide.symptoms])
            )
            common = terms & query_terms
            if not common:
                continue
            similarity = len(common) / len(terms | query_terms)
            if similarity < 0.12:
                continue
            matches.append(
                GuideMatch(
                    guide=guide,
                    similarity=round(similarity, 4),
                    matched_terms=sorted(common)[:10],
                )
            )
        matches.sort(
            key=lambda item: (
                item.similarity,
                item.guide.updated_at,
                item.guide.guide_id,
            ),
            reverse=True,
        )
        selected = []
        used_bytes = 2
        for match in matches:
            size = len(match.model_dump_json().encode("utf-8")) + 1
            if used_bytes + size > 98_304:
                continue
            selected.append(match)
            used_bytes += size
            if len(selected) == 3:
                break
        return GuideLookup(
            next_action="confirm_similarity" if selected else "diagnose",
            matches=selected,
        )

    def import_manual(self, request: GuideImportRequest) -> GuideList:
        clean = GuideImportRequest.model_validate(
            sanitize_data(request.model_dump(mode="json"))
        )
        guides = [
            DiagnosisGuide(
                **content.model_dump(), guide_id=f"guide_{uuid4().hex}", origin="manual"
            )
            for content in clean.guides
        ]
        self.store.import_guides(guides, clean.tenant_id or "")
        return GuideList(items=guides, total=len(guides))

    def update(self, guide_id: str, request: GuideUpdateRequest) -> DiagnosisGuide:
        clean = GuideUpdateRequest.model_validate(
            sanitize_data(request.model_dump(mode="json"))
        )
        return self.store.update_guide(guide_id, clean)

    def list(
        self, *, category: str | None = None, limit: int = 100, offset: int = 0
    ) -> GuideList:
        if category is not None:
            ProblemCategory(category)
        return self.store.list_guides(category=category, limit=limit, offset=offset)

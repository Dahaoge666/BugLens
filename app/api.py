"""HTTP presentation and dependency composition; business operations live in service.py."""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .agents import OpenAINodeRunner
from .graph import DiagnosisGraph
from .models import (
    CreateDiagnosisRequest,
    DiagnosisState,
    GraphContractError,
    SubmitAnswersRequest,
)
from .prompts import PromptRegistry
from .service import DiagnosisFailed, DiagnosisService
from .storage import JsonStateStore, RunNotFoundError


def response_payload(state: DiagnosisState) -> dict:
    base: dict = {
        "run_id": state.run_id,
        "status": state.status,
        "attempts": state.attempt,
    }
    if state.status == "awaiting_user_input":
        base["interaction_request"] = (
            state.pending_interaction.model_dump(mode="json")
            if state.pending_interaction
            else None
        )
        return base
    if state.analysis:
        base["category"] = state.analysis.category.value
    if state.report:
        report = state.report.model_dump(mode="json")
        # The stable API contract exposes a structured conclusion below; the narrative
        # form remains useful to clients under a non-ambiguous key.
        report["primary_conclusion_summary"] = report.pop("primary_conclusion")
        base.update(report)
    if state.investigation:
        primary = (
            next(
                (
                    item
                    for item in state.investigation.hypotheses
                    if item.cause == state.investigation.primary_conclusion
                ),
                None,
            )
            if state.status == "completed"
            else None
        )
        base["primary_conclusion"] = (
            {
                "cause": primary.cause,
                "confidence": primary.confidence,
                "evidence": primary.supporting_evidence,
                "verification_steps": primary.verification_steps,
            }
            if primary
            else None
        )
        base["alternative_hypotheses"] = [
            item.model_dump(mode="json")
            for item in state.investigation.hypotheses
            if item is not primary
        ]
        base["evidence_gaps"] = state.investigation.evidence_gaps
    if state.evaluation:
        base["evaluation"] = state.evaluation.model_dump(mode="json")
    return base


def create_app(service: DiagnosisService | None = None) -> FastAPI:
    if service is None:
        prompts = PromptRegistry()
        service = DiagnosisService(
            DiagnosisGraph(OpenAINodeRunner(prompts), JsonStateStore(), prompts)
        )
    app = FastAPI(title="BugLens", version="0.1.0")

    @app.exception_handler(GraphContractError)
    async def contract_error(request: Request, exc: GraphContractError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(RunNotFoundError)
    async def missing_run(request: Request, exc: RunNotFoundError) -> JSONResponse:
        return JSONResponse(
            status_code=404, content={"detail": "Diagnosis run not found"}
        )

    @app.exception_handler(DiagnosisFailed)
    async def node_failure(request: Request, exc: DiagnosisFailed) -> JSONResponse:
        return JSONResponse(
            status_code=502,
            content={
                "detail": {"run_id": exc.run_id, "message": str(exc)},
            },
        )

    @app.post("/diagnoses", status_code=201)
    async def create_diagnosis(request: CreateDiagnosisRequest) -> dict:
        return response_payload(await service.create(request))

    @app.post("/diagnoses/{run_id}/answers")
    async def submit_answers(run_id: str, request: SubmitAnswersRequest) -> dict:
        return response_payload(await service.answer(run_id, request))

    @app.get("/diagnoses/{run_id}")
    async def get_diagnosis(run_id: str) -> dict:
        return response_payload(service.get(run_id))

    @app.post("/diagnoses/{run_id}/cancel")
    async def cancel_diagnosis(run_id: str) -> dict:
        return response_payload(await service.cancel(run_id))

    return app


app = create_app()

"""Thin interactive CLI adapter over the shared AgentClient protocol."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from uuid import uuid4

from .application import ModelCredentialsError
from .bootstrap import build_local_client
from .client import AgentClient, RemoteAgentClient
from .config import Settings
from .models import (
    Evidence,
    LifecycleStatus,
    PendingTargetConfirmation,
    TargetSpec,
    UserAnswer,
    UserInteractionRequest,
)
from .protocol.commands import (
    CancelDiagnosis,
    ConfirmDiagnosisTarget,
    ResumeDiagnosis,
    SkipUserInteraction,
    StartDiagnosis,
    SubmitUserAnswers,
)
from .protocol.events import (
    InputRequired,
    RunWaiting,
    TargetConfirmationRequired,
)


class NonInteractiveClarifierError(RuntimeError):
    """Raised when clarification is required but stdin is not interactive."""

    code = "non_interactive_clarifier"


class ConsoleClarifier:
    def __init__(self, *, stream=None) -> None:
        # Default to stdin; allow injection for tests.
        self._stream = stream if stream is not None else sys.stdin

    async def ask(self, request: UserInteractionRequest) -> list[UserAnswer]:
        if not self._stream.isatty():
            raise NonInteractiveClarifierError(
                "diagnosis requires clarification but stdin is not a TTY; "
                "re-run in an interactive terminal, or use --resume RUN_ID (or the "
                "Web adapter) to supply answers separately"
            )
        print(f"\n需要补充信息：{request.explanation}")
        answers: list[UserAnswer] = []
        for question in request.questions:
            print(f"\n{question.question}")
            print(f"原因：{question.rationale}")
            if question.options:
                print("选项：" + " / ".join(question.options))
            answer = input("> ").strip()
            answers.append(
                UserAnswer(
                    request_id=request.request_id,
                    question_id=question.id,
                    answer=answer,
                )
            )
        return answers

    def choose_target(
        self, request: TargetConfirmationRequired | PendingTargetConfirmation
    ) -> str:
        if not self._stream.isatty():
            raise NonInteractiveClarifierError(
                "diagnosis requires environment confirmation but stdin is not a TTY; "
                "use the Web adapter or re-run in an interactive terminal"
            )
        print("\n请确认本次诊断的环境：")
        for index, candidate in enumerate(request.candidates, start=1):
            detail = f"{candidate.level} · {candidate.timezone}"
            if candidate.region:
                detail += f" · {candidate.region}"
            print(
                f"{index}. {candidate.display_name} ({candidate.environment_id}) [{detail}]"
            )
        while True:
            answer = input("选择编号：").strip()
            try:
                selected = request.candidates[int(answer) - 1]
            except (ValueError, IndexError):
                print("请输入候选项编号。")
                continue
            return selected.environment_id


def _pairs(values: list[str], option: str) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    for value in values:
        if "=" not in value:
            raise ValueError(f"{option} requires KEY=VALUE")
        key, item = value.split("=", 1)
        if not key or not item:
            raise ValueError(f"{option} requires non-empty KEY and VALUE")
        result.append((key, item))
    return result


def _key_values(values: list[str], option: str) -> dict[str, str]:
    return dict(_pairs(values, option))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="buglens",
        description="Run an evidence-bound, resumable issue diagnosis.",
    )
    parser.add_argument("question", nargs="?", help="problem description")
    parser.add_argument("--context", action="append", default=[], metavar="KEY=VALUE")
    parser.add_argument(
        "--evidence", action="append", default=[], metavar="SOURCE=TEXT"
    )
    parser.add_argument("--tenant", help="tenant prompt configuration key")
    parser.add_argument(
        "--environment",
        dest="environment_id",
        help="明确选择环境 ID；不传时使用 context 提示并请求确认",
    )
    parser.add_argument("--service", dest="service_id", help="主服务 ID 或名称提示")
    parser.add_argument(
        "--infer-environment",
        action="store_true",
        help="显式要求按环境/服务提示推断并确认",
    )
    parser.add_argument("--profile", default=None, help="versioned runtime profile")
    parser.add_argument(
        "--config", type=Path, help="local administrator runtime config"
    )
    parser.add_argument(
        "--session-db", type=Path, help="local checkpoint and SDK database"
    )
    parser.add_argument("--remote", help="remote BugLens base URL")
    parser.add_argument("--resume", metavar="RUN_ID", help="resume a waiting run")
    parser.add_argument("--skip", metavar="RUN_ID", help="skip pending clarification")
    parser.add_argument("--status", metavar="RUN_ID", help="show a run")
    parser.add_argument("--cancel", metavar="RUN_ID", help="cancel a run")
    parser.add_argument(
        "--json", action="store_true", help="print the final state as JSON"
    )
    parser.add_argument(
        "--output", type=Path, metavar="FILE", help="write state JSON to a file"
    )
    return parser


def _settings(args: argparse.Namespace) -> Settings:
    settings = Settings.from_env()
    values = settings.model_dump()
    if args.session_db:
        values["session_db"] = args.session_db
    if args.config:
        values["config_path"] = args.config
    if args.remote:
        values["remote"] = args.remote
    return Settings.model_validate(values)


async def _client(
    settings: Settings,
) -> tuple[AgentClient, object | None, object | None]:
    if settings.remote:
        return RemoteAgentClient(settings.remote), None, None
    return build_local_client(settings)


async def _send_interactively(
    client: AgentClient,
    command: StartDiagnosis | SubmitUserAnswers | ConfirmDiagnosisTarget,
    clarifier: ConsoleClarifier,
) -> None:
    current = command
    while True:
        pending: InputRequired | None = None
        target_pending: TargetConfirmationRequired | None = None
        waiting: RunWaiting | None = None
        async for event in client.send(current):
            if isinstance(event, InputRequired):
                pending = event
            if isinstance(event, TargetConfirmationRequired):
                target_pending = event
            if isinstance(event, RunWaiting):
                waiting = event
        if target_pending is not None:
            current = ConfirmDiagnosisTarget(
                run_id=target_pending.run_id,
                expected_revision=(
                    waiting.revision if waiting else target_pending.revision
                ),
                request_id=target_pending.request_id,
                environment_id=clarifier.choose_target(target_pending),
            )
            continue
        if pending is None:
            return
        answers = await clarifier.ask(pending.request)
        current = SubmitUserAnswers(
            run_id=pending.run_id,
            expected_revision=(waiting.revision if waiting else pending.revision),
            request_id=pending.request.request_id,
            answers=answers,
        )


async def run_cli(
    args: argparse.Namespace, clarifier: ConsoleClarifier | None = None
) -> int:
    settings = _settings(args)
    client, store, runner = await _client(settings)
    try:
        run_id = args.resume or args.status or args.cancel or args.skip
        if args.status:
            state = await client.get_run(args.status)
        elif args.cancel:
            state = await client.get_run(args.cancel)
            if "cancel" not in state.available_actions:
                raise ValueError("run cannot be cancelled in its current state")
            async for _ in client.send(
                CancelDiagnosis(
                    run_id=args.cancel,
                    expected_revision=state.revision,
                    reason="cancelled from CLI",
                )
            ):
                pass
            state = await client.get_run(args.cancel)
        elif args.skip:
            state = await client.get_run(args.skip)
            if (
                "skip_input" not in state.available_actions
                or not state.pending_interaction
            ):
                raise ValueError("run cannot skip input in its current state")
            async for _ in client.send(
                SkipUserInteraction(
                    run_id=args.skip,
                    expected_revision=state.revision,
                    request_id=state.pending_interaction.request_id,
                    reason="skipped from CLI",
                )
            ):
                pass
            state = await client.get_run(args.skip)
        elif args.resume:
            state = await client.get_run(args.resume)
            if (
                "confirm_target" in state.available_actions
                and state.pending_target_confirmation is not None
            ):
                pending_target = state.pending_target_confirmation
                await _send_interactively(
                    client,
                    ConfirmDiagnosisTarget(
                        run_id=args.resume,
                        expected_revision=state.revision,
                        request_id=pending_target.request_id,
                        environment_id=(clarifier or ConsoleClarifier()).choose_target(
                            pending_target
                        ),
                    ),
                    clarifier or ConsoleClarifier(),
                )
                state = await client.get_run(args.resume)
            elif "resume" in state.available_actions:
                async for _ in client.send(
                    ResumeDiagnosis(
                        run_id=args.resume,
                        expected_revision=state.revision,
                    )
                ):
                    pass
                state = await client.get_run(args.resume)
            elif (
                "submit_answers" in state.available_actions
                and state.pending_interaction
            ):
                await _send_interactively(
                    client,
                    SubmitUserAnswers(
                        run_id=args.resume,
                        expected_revision=state.revision,
                        request_id=state.pending_interaction.request_id,
                        answers=await (clarifier or ConsoleClarifier()).ask(
                            state.pending_interaction
                        ),
                    ),
                    clarifier or ConsoleClarifier(),
                )
                state = await client.get_run(args.resume)
            else:
                raise ValueError("run has no resumable action")
        else:
            context = _key_values(args.context, "--context")
            if args.tenant:
                context["tenant_id"] = args.tenant
            evidence = [
                Evidence(source=s, content=c)
                for s, c in _pairs(args.evidence, "--evidence")
            ]
            question = args.question or input("问题描述：").strip()
            if not question:
                raise ValueError("question cannot be empty")
            start = StartDiagnosis(
                run_id=uuid4().hex,
                question=question,
                context=context,
                evidence=evidence,
                profile=args.profile or settings.default_profile,
                target=TargetSpec(
                    mode=(
                        "infer"
                        if args.infer_environment
                        else ("explicit" if args.environment_id else "infer")
                    ),
                    environment_id=args.environment_id,
                    primary_service_id=args.service_id,
                ),
            )
            await _send_interactively(client, start, clarifier or ConsoleClarifier())
            state = await client.get_run(start.run_id)
            run_id = start.run_id
        result_json = state.model_dump_json(indent=2)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(result_json + "\n", encoding="utf-8")
        if args.json:
            print(result_json)
        else:
            print(f"\n运行 ID：{run_id or state.run_id}")
            print(f"生命周期：{state.lifecycle_status.value}")
            if state.outcome:
                print(f"结果：{state.outcome.value}")
            if state.report:
                print(f"结论：{state.report.executive_summary}")
                if state.report.primary_conclusion:
                    print(f"主要原因：{state.report.primary_conclusion}")
                print(state.report.status_explanation)
        return (
            0
            if state.lifecycle_status == LifecycleStatus.COMPLETED
            and state.outcome == "confirmed"
            else 2
        )
    finally:
        service = getattr(client, "service", None)
        if service is not None:
            service.close()
        if runner is not None:
            runner.close()
        if store is not None:
            store.close()


def main() -> None:
    try:
        code = asyncio.run(run_cli(build_parser().parse_args()))
    except (KeyboardInterrupt, EOFError):
        print("\n已取消。")
        code = 130
    except NonInteractiveClarifierError as exc:
        # Keep the run in waiting_user so it can be resumed with answers via --resume
        # or the Web adapter; do not treat it as a generic cancellation.
        print(f"\n需要补充信息但当前环境不支持交互输入：{exc}")
        code = 64
    except ModelCredentialsError as exc:
        print(f"\n模型凭据未就绪：{exc}")
        code = 66
    except ValueError as exc:
        print(f"输入错误：{exc}")
        code = 2
    raise SystemExit(code)

"""Interactive command-line interface."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from .agents import NodeExecutionError, OpenAINodeRunner
from .config import Settings
from .graph import Clarifier, DiagnosisGraph
from .models import (
    Evidence,
    UserAnswer,
    UserInteractionRequest,
)
from .prompts import PromptRegistry


class ConsoleClarifier(Clarifier):
    async def ask(self, request: UserInteractionRequest) -> list[UserAnswer]:
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
        description="Run an evidence-bound, interactive issue diagnosis.",
    )
    parser.add_argument("question", nargs="?", help="problem description")
    parser.add_argument("--context", action="append", default=[], metavar="KEY=VALUE")
    parser.add_argument(
        "--evidence", action="append", default=[], metavar="SOURCE=TEXT"
    )
    parser.add_argument("--tenant", help="tenant prompt configuration key")
    parser.add_argument("--session-db", type=Path, help="SDK SQLite session database")
    parser.add_argument("--max-attempts", type=int, choices=(1, 2), default=2)
    parser.add_argument(
        "--max-clarifications", type=int, choices=range(0, 11), default=2
    )
    parser.add_argument(
        "--json", action="store_true", help="print the final state as JSON"
    )
    parser.add_argument(
        "--output",
        type=Path,
        metavar="FILE",
        help="write the complete diagnosis state to a JSON file",
    )
    return parser


async def run_cli(args: argparse.Namespace, clarifier: Clarifier | None = None) -> int:
    settings = Settings.from_env()
    context = _key_values(args.context, "--context")
    if args.tenant:
        context["tenant_id"] = args.tenant
    evidence = [
        Evidence(source=source, content=content)
        for source, content in _pairs(args.evidence, "--evidence")
    ]
    question = args.question or input("问题描述：").strip()
    if not question:
        raise ValueError("question cannot be empty")

    prompts = PromptRegistry(settings.prompt_config)
    runner = OpenAINodeRunner(prompts, args.session_db or settings.session_db)
    graph = DiagnosisGraph(
        runner,
        prompts,
        tracing_enabled=settings.tracing_enabled,
    )
    try:
        result = await graph.run(
            question=question,
            context=context,
            evidence=evidence,
            clarifier=clarifier or ConsoleClarifier(),
            max_attempts=args.max_attempts,
            max_clarification_rounds=args.max_clarifications,
        )
    finally:
        runner.close()

    result_json = result.model_dump_json(indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(result_json + "\n", encoding="utf-8")

    if args.json:
        print(result_json)
    elif result.report:
        print(f"\n运行 ID：{result.run_id}")
        print(f"状态：{result.status}")
        print(f"结论：{result.report.executive_summary}")
        if result.report.primary_conclusion:
            print(f"主要原因：{result.report.primary_conclusion}")
        print(result.report.status_explanation)
        if result.report.next_actions:
            print("后续动作：")
            for action in result.report.next_actions:
                print(f"  - {action}")
        if args.output:
            print(f"完整结果：{args.output.resolve()}")
    return 0 if result.status == "completed" else 2


def main() -> None:
    try:
        code = asyncio.run(run_cli(build_parser().parse_args()))
    except (KeyboardInterrupt, EOFError):
        print("\n已取消。")
        code = 130
    except ValueError as exc:
        print(f"输入错误：{exc}")
        code = 2
    except NodeExecutionError:
        print("Agent 执行失败，请检查 API Key、网络和模型服务状态。")
        code = 1
    raise SystemExit(code)

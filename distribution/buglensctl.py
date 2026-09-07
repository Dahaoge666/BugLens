"""Cross-platform native installer and process manager for BugLens."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

DIST_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = DIST_ROOT.parent
BACKEND_ROOT = PROJECT_ROOT / "backend"
FRONTEND_ROOT = PROJECT_ROOT / "frontend"
RUNTIME_ROOT = DIST_ROOT / ".runtime"
ENV_FILE = RUNTIME_ROOT / ".env"
MODE_FILE = RUNTIME_ROOT / "mode"
CONFIG_FILE = RUNTIME_ROOT / "config.yaml"
DATA_ROOT = RUNTIME_ROOT / "data"
LOG_ROOT = RUNTIME_ROOT / "logs"
PID_ROOT = RUNTIME_ROOT / "pids"
FRONTEND_INSTALL_ROOT = RUNTIME_ROOT / "frontend"
VENV_ROOT = RUNTIME_ROOT / "venv"


def ensure_python() -> None:
    if sys.version_info < (3, 11):
        raise SystemExit("Python 3.11 or newer is required.")


def ensure_runtime() -> None:
    for path in (RUNTIME_ROOT, DATA_ROOT, LOG_ROOT, PID_ROOT):
        path.mkdir(parents=True, exist_ok=True)
    if not ENV_FILE.exists():
        shutil.copy2(DIST_ROOT / ".env.example", ENV_FILE)
        print(f"Created {ENV_FILE}")
    if not CONFIG_FILE.exists():
        shutil.copy2(BACKEND_ROOT / "config" / "buglens.example.yaml", CONFIG_FILE)
        print(f"Created {CONFIG_FILE}")


def load_dotenv() -> dict[str, str]:
    values: dict[str, str] = {}
    if not ENV_FILE.exists():
        return values
    for raw_line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def runtime_environment() -> dict[str, str]:
    env = os.environ.copy()
    env.update(load_dotenv())
    env["BUGLENS_SESSION_DB"] = str(DATA_ROOT / "buglens.db")
    env["BUGLENS_CONFIG"] = str(CONFIG_FILE)
    return env


def venv_python() -> Path:
    folder = "Scripts" if os.name == "nt" else "bin"
    suffix = ".exe" if os.name == "nt" else ""
    return VENV_ROOT / folder / f"python{suffix}"


def backend_executable() -> Path:
    folder = "Scripts" if os.name == "nt" else "bin"
    suffix = ".exe" if os.name == "nt" else ""
    return VENV_ROOT / folder / f"buglens-web{suffix}"


def run_checked(
    arguments: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> None:
    print("+", " ".join(arguments))
    subprocess.run(arguments, cwd=cwd, env=env, check=True)


def install_backend() -> None:
    uv = shutil.which("uv")
    if not uv:
        raise SystemExit("uv is required. Install it from https://docs.astral.sh/uv/.")
    install_env = os.environ.copy()
    install_env["UV_PROJECT_ENVIRONMENT"] = str(VENV_ROOT)
    install_env["UV_PYTHON"] = "3.11"
    run_checked(
        [
            uv,
            "sync",
            "--project",
            str(BACKEND_ROOT),
            "--locked",
            "--no-dev",
            "--extra",
            "web",
        ],
        env=install_env,
    )


def frontend_source() -> Path:
    configured = load_dotenv().get("BUGLENS_FRONTEND_SOURCE", "").strip()
    return (
        Path(configured).expanduser().resolve()
        if configured
        else FRONTEND_ROOT / "dist"
    )


def build_frontend_if_needed(source: Path) -> None:
    if (source / "index.html").exists():
        return
    pnpm = shutil.which("pnpm")
    corepack = shutil.which("corepack")
    if pnpm:
        prefix = [pnpm]
    elif corepack:
        prefix = [corepack, "pnpm"]
    else:
        raise SystemExit(
            "No prebuilt frontend was found. Install Node.js/Corepack to build it, "
            "or set BUGLENS_FRONTEND_SOURCE to a frontend-static release directory."
        )
    run_checked([*prefix, "install", "--frozen-lockfile"], cwd=FRONTEND_ROOT)
    run_checked([*prefix, "build"], cwd=FRONTEND_ROOT)
    if not (source / "index.html").exists():
        raise SystemExit(f"Frontend build did not create {source / 'index.html'}")


def install_frontend() -> None:
    source = frontend_source()
    build_frontend_if_needed(source)
    if FRONTEND_INSTALL_ROOT.exists():
        shutil.rmtree(FRONTEND_INSTALL_ROOT)
    shutil.copytree(source, FRONTEND_INSTALL_ROOT)


def pid_file(name: str) -> Path:
    return PID_ROOT / f"{name}.pid"


def read_pid(name: str) -> int | None:
    try:
        return int(pid_file(name).read_text(encoding="ascii").strip())
    except (FileNotFoundError, ValueError):
        return None


def process_running(pid: int | None) -> bool:
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def process_matches(pid: int, marker: str) -> bool:
    if os.name != "nt":
        try:
            command = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ")
            return marker.encode() in command
        except OSError:
            return False
    escaped = marker.replace("'", "''")
    command = (
        f"$p=Get-CimInstance Win32_Process -Filter 'ProcessId={pid}';"
        f"if ($p.CommandLine -like '*{escaped}*') {{ exit 0 }} else {{ exit 1 }}"
    )
    return (
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode
        == 0
    )


def spawn(name: str, arguments: list[str], env: dict[str, str], marker: str) -> None:
    current = read_pid(name)
    if process_running(current):
        assert current is not None
        if process_matches(current, marker):
            print(f"{name} is already running (PID {current}).")
            return
        raise SystemExit(
            f"Recorded PID {current} belongs to another process; remove "
            f"{pid_file(name)} after verification."
        )
    pid_file(name).unlink(missing_ok=True)
    log_path = LOG_ROOT / f"{name}.log"
    log = log_path.open("ab")
    options: dict[str, object] = {
        "cwd": str(PROJECT_ROOT),
        "env": env,
        "stdin": subprocess.DEVNULL,
        "stdout": log,
        "stderr": subprocess.STDOUT,
    }
    if os.name == "nt":
        options["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        )
    else:
        options["start_new_session"] = True
    process = subprocess.Popen(arguments, **options)
    log.close()
    pid_file(name).write_text(str(process.pid), encoding="ascii")
    time.sleep(0.3)
    if not process_running(process.pid) or not process_matches(process.pid, marker):
        raise SystemExit(f"{name} failed to start; inspect {log_path}")
    print(f"Started {name} (PID {process.pid}).")


def stop_process(name: str, marker: str) -> None:
    pid = read_pid(name)
    if not process_running(pid):
        pid_file(name).unlink(missing_ok=True)
        print(f"{name} is not running.")
        return
    assert pid is not None
    if not process_matches(pid, marker):
        raise SystemExit(
            f"Refusing to stop PID {pid}: it does not match the recorded {name} process."
        )
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T"], check=False)
    else:
        os.kill(pid, signal.SIGTERM)
    for _ in range(30):
        if not process_running(pid):
            break
        time.sleep(0.1)
    if process_running(pid):
        raise SystemExit(f"{name} did not stop; PID file was preserved.")
    pid_file(name).unlink(missing_ok=True)
    print(f"Stopped {name}.")


def selected_mode(requested: str | None) -> str:
    if requested:
        return requested
    if MODE_FILE.exists():
        saved = MODE_FILE.read_text(encoding="ascii").strip()
        if saved in {"backend", "full"}:
            return saved
    return "full"


def health_url(mode: str, env: dict[str, str]) -> str:
    def client_host(value: str) -> str:
        return "127.0.0.1" if value in {"0.0.0.0", "::"} else value

    if mode == "full":
        return (
            f"http://{client_host(env.get('BUGLENS_WEB_HOST', '127.0.0.1'))}:"
            f"{env.get('BUGLENS_WEB_PORT', '8080')}/v1/admin/health"
        )
    return (
        f"http://{client_host(env.get('BUGLENS_HOST', '127.0.0.1'))}:"
        f"{env.get('BUGLENS_PORT', '8000')}/v1/admin/health"
    )


def wait_for_health(mode: str, env: dict[str, str]) -> None:
    url = health_url(mode, env)
    for _ in range(40):
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status == 200:
                    print(f"BugLens is ready: {url.removesuffix('/v1/admin/health')}")
                    return
        except (OSError, urllib.error.URLError):
            time.sleep(0.25)
    raise SystemExit(f"Health check failed: {url}. Inspect logs in {LOG_ROOT}")


def start(mode: str) -> None:
    if not backend_executable().exists():
        raise SystemExit("BugLens is not installed. Run install first.")
    if mode == "full" and not (FRONTEND_INSTALL_ROOT / "index.html").exists():
        raise SystemExit("Frontend is not installed. Run install --mode full.")
    env = runtime_environment()
    host = env.get("BUGLENS_HOST", "127.0.0.1")
    port = env.get("BUGLENS_PORT", "8000")
    spawn(
        "backend",
        [str(backend_executable()), "--host", host, "--port", port],
        env,
        "buglens-web",
    )
    if mode == "full":
        web_host = env.get("BUGLENS_WEB_HOST", "127.0.0.1")
        web_port = env.get("BUGLENS_WEB_PORT", "8080")
        spawn(
            "frontend",
            [
                str(venv_python()),
                str(DIST_ROOT / "spa_server.py"),
                "--directory",
                str(FRONTEND_INSTALL_ROOT),
                "--host",
                web_host,
                "--port",
                web_port,
                "--backend-host",
                "127.0.0.1" if host in {"0.0.0.0", "::"} else host,
                "--backend-port",
                port,
            ],
            env,
            "spa_server.py",
        )
    MODE_FILE.write_text(mode, encoding="ascii")
    wait_for_health(mode, env)


def stop() -> None:
    stop_process("frontend", "spa_server.py")
    stop_process("backend", "buglens-web")


def backup_runtime() -> Path:
    target = RUNTIME_ROOT / "backups" / datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    target.mkdir(parents=True)
    for source in (ENV_FILE, CONFIG_FILE):
        if source.exists():
            shutil.copy2(source, target / source.name)
    if DATA_ROOT.exists():
        shutil.copytree(DATA_ROOT, target / "data")
    return target


def status(mode: str) -> None:
    for name in ("backend", "frontend"):
        pid = read_pid(name)
        state = f"running (PID {pid})" if process_running(pid) else "stopped"
        if name == "frontend" and mode == "backend":
            state = "not selected"
        print(f"{name}: {state}")


def doctor(mode: str) -> None:
    ensure_runtime()
    print(f"Python: {sys.version.split()[0]}")
    print(f"Runtime: {RUNTIME_ROOT}")
    status(mode)
    try:
        with urllib.request.urlopen(
            health_url(mode, runtime_environment()), timeout=3
        ) as response:
            payload = json.load(response)
        print(f"Health: {payload.get('status', 'unknown')}")
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        print(f"Health: unreachable ({exc})")
        raise SystemExit(1) from exc


def uninstall_runtime() -> None:
    stop()
    for target in (VENV_ROOT, FRONTEND_INSTALL_ROOT, LOG_ROOT, PID_ROOT):
        if target.exists():
            shutil.rmtree(target)
    print(
        f"Removed installed programs. Data and configuration remain in {RUNTIME_ROOT}."
    )


def main() -> None:
    ensure_python()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        nargs="?",
        default="help",
        choices=(
            "install",
            "start",
            "stop",
            "restart",
            "update",
            "status",
            "doctor",
            "uninstall",
            "init",
            "help",
        ),
    )
    parser.add_argument("--mode", choices=("backend", "full"), default=None)
    args = parser.parse_args()
    mode = selected_mode(args.mode)

    try:
        if args.command == "help":
            parser.print_help()
            return
        ensure_runtime()
        if args.command == "init":
            print(f"Edit {ENV_FILE}, then run install --mode backend or full.")
        elif args.command == "install":
            install_backend()
            if mode == "full":
                install_frontend()
            start(mode)
        elif args.command == "start":
            start(mode)
        elif args.command == "stop":
            stop()
        elif args.command == "restart":
            stop()
            start(mode)
        elif args.command == "update":
            stop()
            backup = backup_runtime()
            install_backend()
            if mode == "full":
                install_frontend()
            start(mode)
            print(f"Backup: {backup}")
        elif args.command == "status":
            status(mode)
        elif args.command == "doctor":
            doctor(mode)
        elif args.command == "uninstall":
            uninstall_runtime()
    except subprocess.CalledProcessError as exc:
        raise SystemExit(f"Command failed with exit code {exc.returncode}.") from None
    except OSError as exc:
        raise SystemExit(f"Local operation failed: {exc}") from None


if __name__ == "__main__":
    main()

"""Run the backend and frontend development servers as one foreground command."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import webbrowser

from trade_compass_agent.config import PROJECT_ROOT, is_source_checkout
from trade_compass_agent.daemon.status import probe_port

DEV_HOST = "127.0.0.1"
DEV_WEB_PORT = 3000


def _fail(message: str) -> None:
    print(f"Error: {message}", file=sys.stderr)
    raise SystemExit(1)


def _open_browser_later(url: str) -> None:
    time.sleep(1)
    webbrowser.open(url)


def _send_process_group_signal(process: subprocess.Popen, sig: signal.Signals) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, sig)
        elif sig == signal.SIGTERM:
            process.terminate()
        else:
            process.kill()
    except OSError:
        pass


def _shutdown_dev_processes(processes: list[subprocess.Popen]) -> None:
    running = [process for process in processes if process.poll() is None]
    for process in running:
        _send_process_group_signal(process, signal.SIGTERM)
    for process in running:
        try:
            process.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            _send_process_group_signal(process, signal.SIGKILL)
            process.wait(timeout=1.0)


def run_development(*, api_port: int, open_browser: bool, no_scheduler: bool) -> None:
    """Start and supervise the existing backend and Vite development commands."""
    web_dir = PROJECT_ROOT / "apps" / "web"
    if not is_source_checkout() or not (web_dir / "package.json").is_file():
        _fail("the dev command is only available from a source checkout")

    pnpm = shutil.which("pnpm")
    if not pnpm:
        _fail("pnpm is required; install it and run `pnpm install --frozen-lockfile`")

    if api_port == DEV_WEB_PORT:
        _fail(f"the API and Web UI cannot both use port {api_port}")
    if probe_port(DEV_HOST, api_port):
        _fail(
            f"API port {api_port} is already in use. Keep the persistent service running "
            "and choose an unused development port with --port"
        )
    if probe_port(DEV_HOST, DEV_WEB_PORT):
        _fail(f"Web UI port {DEV_WEB_PORT} is already in use")

    backend_command = [
        sys.executable,
        "-m",
        "trade_compass_agent.cli",
        "serve",
        "--host",
        DEV_HOST,
        "--port",
        str(api_port),
        "--dev",
    ]
    if no_scheduler:
        backend_command.append("--no-scheduler")
    frontend_command = [
        pnpm,
        "--dir",
        str(web_dir),
        "run",
        "dev",
        "--strictPort",
    ]

    child_env = os.environ.copy()
    child_env["TRADE_COMPASS_DEV_SUPERVISED"] = "true"
    child_env["TRADE_COMPASS_API_ORIGIN"] = f"http://{DEV_HOST}:{api_port}"
    popen_kwargs: dict[str, object] = {
        "cwd": str(PROJECT_ROOT),
        "env": child_env,
    }
    if os.name == "posix":
        popen_kwargs["start_new_session"] = True

    api_url = f"http://{DEV_HOST}:{api_port}"
    web_url = f"http://{DEV_HOST}:{DEV_WEB_PORT}/agent"
    print("Trade Compass development servers")
    print(f"  Web UI: {web_url}")
    print(f"  API:    {api_url}")
    print("  Press Ctrl+C to stop both servers.")

    processes: list[subprocess.Popen] = []
    exit_code = 0
    try:
        backend = subprocess.Popen(backend_command, **popen_kwargs)
        processes.append(backend)
        frontend = subprocess.Popen(frontend_command, **popen_kwargs)
        processes.append(frontend)
        if open_browser:
            threading.Thread(target=_open_browser_later, args=(web_url,), daemon=True).start()

        while True:
            for name, process in (("backend", backend), ("frontend", frontend)):
                code = process.poll()
                if code is not None:
                    exit_code = code
                    stream = sys.stderr if code else sys.stdout
                    print(f"{name} development server exited with code {code}", file=stream)
                    break
            else:
                time.sleep(0.2)
                continue
            break
    except KeyboardInterrupt:
        print("\nStopping development servers...")
    except OSError as exc:
        print(f"Error: failed to start development servers: {exc}", file=sys.stderr)
        exit_code = 1
    finally:
        _shutdown_dev_processes(processes)

    if exit_code:
        raise SystemExit(exit_code)

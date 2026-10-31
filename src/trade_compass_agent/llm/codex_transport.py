"""Bounded stdio JSON-RPC transport for the installed Codex app-server."""

from __future__ import annotations

import json
import os
import queue
import selectors
import signal
import subprocess
import threading
import time
from collections import deque

from trade_compass_agent.runtime.exceptions import AgentUnavailableError
from trade_compass_agent.llm.native_environment import native_agent_environment


class CodexRPCError(AgentUnavailableError):
    def __init__(self, method, error):
        super().__init__(f"Codex 无法完成 {method}，请检查 CLI 版本、登录和配置。")
        message = str(error.get("message", "")).lower()
        self.missing_thread = method == "thread/resume" and any(
            phrase in message for phrase in ("no rollout found", "thread not found", "thread does not exist")
        )


class CodexTransport:
    def __init__(self, binary, cwd, check):
        self.check = check
        self.events = queue.Queue()
        self.sequence = 0
        self.backlog = deque()
        # Keep the vendor's credential store and provider configuration, but
        # disable host capabilities. Mutations go through application tools.
        args = [binary, "app-server"]
        for feature in ("shell_tool", "unified_exec", "plugins", "apps", "hooks", "multi_agent",
                        "memories", "code_mode", "browser_use", "computer_use", "image_generation", "view_image",
                        "unbounded_connection_retries"):
            args.extend(["-c", f"features.{feature}=false"])
        args.extend(["-c", "web_search=\"disabled\"", "-c", "project_doc_max_bytes=0"])
        self.process = subprocess.Popen(args, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, text=True, encoding="utf-8", start_new_session=True,
                                        env=native_agent_environment())
        os.set_blocking(self.process.stdin.fileno(), False)
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        try:
            for line in self.process.stdout:
                try:
                    self.events.put(json.loads(line))
                except ValueError:
                    self.events.put({"transport_error": True})
                    break
        finally:
            self.events.put(None)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        if self.process.poll() is None:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=2)
            except ProcessLookupError:
                pass
        self.reader.join(timeout=1)
        for pipe in (self.process.stdin, self.process.stdout):
            if pipe:
                pipe.close()

    def send(self, value, *, check_active=True):
        payload = memoryview((json.dumps(value, ensure_ascii=False) + "\n").encode("utf-8"))
        cleanup_deadline = time.monotonic() + .2
        try:
            fd = self.process.stdin.fileno()
            with selectors.DefaultSelector() as selector:
                selector.register(fd, selectors.EVENT_WRITE)
                while payload:
                    if check_active:
                        self.check()
                    elif time.monotonic() >= cleanup_deadline:
                        raise AgentUnavailableError("Codex 未响应中断请求，本轮进程将关闭。")
                    if not selector.select(timeout=.1):
                        continue
                    try:
                        written = os.write(fd, payload[:65536])
                    except BlockingIOError:
                        continue
                    payload = payload[written:]
        except (BrokenPipeError, OSError):
            raise AgentUnavailableError("Codex 已断开，请检查登录和 CLI 版本后重试。") from None

    def receive(self):
        if self.backlog:
            self.check()
            return self.backlog.popleft()
        return self._receive()

    def _receive(self):
        while True:
            self.check()
            try:
                event = self.events.get(timeout=.1)
            except queue.Empty:
                continue
            if event is None or event.get("transport_error"):
                raise AgentUnavailableError("Codex 连接中断，请检查 CLI 版本和登录状态。")
            return event

    def call(self, method, params):
        self.sequence += 1
        request_id = self.sequence
        self.send({"id": request_id, "method": method, "params": params})
        while True:
            event = self._receive()
            if event.get("id") == request_id and "method" not in event:
                if "error" in event:
                    raise CodexRPCError(method, event["error"])
                return event.get("result", {})
            if "id" in event and "method" in event:
                self.send({"id": event["id"], "error": {"code": -32601,
                          "message": "Interactive requests are not available"}})
            elif "method" in event:
                self.backlog.append(event)

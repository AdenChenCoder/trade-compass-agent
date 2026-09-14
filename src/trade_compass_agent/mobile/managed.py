"""The desktop process owns the provider child and its mobile-only TLS listener."""
import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
import re
import stat
from urllib.parse import urlsplit
from uuid import uuid4

from trade_compass_agent.concurrency import atomic_write
from trade_compass_agent.mobile.helper import helper_binary, private_directory
from trade_compass_agent.mobile.reachability import PublicReachability

STATUS_MAX_AGE = 15

MESSAGES = {
    "starting": "正在准备手机连接…",
    "needs_login_or_network": "请登录 Tailscale 账号；已登录时请检查电脑网络。",
    "needs_funnel_https_permission": "请允许 Tailscale 为这台电脑提供 HTTPS 连接。",
    "preparing_certificate": "正在准备安全连接地址，首次使用可能需要一点时间。",
    "waiting_for_mobile": "正在启动手机入口…",
    "control_status_unavailable": "暂时无法连接服务，正在等待网络恢复。",
    "ready": "手机连接已开启。用手机扫码后，输入电脑显示的配对码即可连接。",
    "error": "手机连接已中断，请检查网络后重试。原会话和手机授权仍会保留。",
    "stopped": "手机连接已关闭。",
}


def provider_action(raw):
    try:
        url = urlsplit(raw)
        return raw if (url.scheme == "https" and url.hostname in {"login.tailscale.com", "controlplane.tailscale.com"}
                       and not url.username and not url.password and url.port is None) else ""
    except (TypeError, ValueError):
        return ""


class ManagedConnection:
    def __init__(self, app, config):
        self.app, self.config = app, config
        self.directory = config.data_dir / "mobile" / "funnel"
        self.instance = uuid4().hex
        self.bridge = self.directory / ("mobile-" + self.instance + ".json")
        self.phase, self.action, self.error = "starting", "", ""
        self.process = self.task = self.context = None
        self.reachability = None
        self.stop = asyncio.Event()

    def status(self):
        return {"phase": self.phase, "action_url": self.action or None,
                "message": self.error or MESSAGES[self.phase],
                "public_check": self.reachability.status() if self.reachability else None}

    async def start(self):
        executable = await asyncio.to_thread(helper_binary, self.config.data_dir / "mobile")
        private_directory(self.directory)
        # Provider subprocesses never inherit Agent/model credentials or provider overrides.
        env = {key: os.environ[key] for key in ("HOME", "USER", "LOGNAME", "PATH", "LANG", "TMPDIR") if key in os.environ}
        self.process = await asyncio.create_subprocess_exec(str(executable), "--state-dir", str(self.directory),
            "--connect", "--mobile-bridge-config", str(self.bridge), "--wait-for-mobile",
            "--parent-stdin", "--instance", self.instance, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, env=env,
            cwd=self.directory, start_new_session=True)
        self.task = asyncio.create_task(self._watch(), name="mobile-connection")

    def _read(self):
        path = self.directory / "status.json"
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 8192:
            raise ValueError("invalid provider status")
        value = json.loads(path.read_text())
        # A previous helper's ready/login record must never be reused after restart.
        if value.get("instance") != self.instance:
            return None
        if not isinstance(value.get("updated_at"), str):
            raise ValueError("invalid provider timestamp")
        updated = datetime.fromisoformat(value["updated_at"].replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - updated).total_seconds()
        if not -5 <= age <= STATUS_MAX_AGE:
            return {"phase": "control_status_unavailable"}
        return value

    async def _watch(self):
        try:
            while not self.stop.is_set():
                if self.process.returncode is not None:
                    raise RuntimeError(MESSAGES["error"])
                try:
                    state = await asyncio.to_thread(self._read)
                except FileNotFoundError:
                    state = None
                if state:
                    phase = state["phase"]
                    if phase == "waiting_for_mobile" and self.context is None:
                        await self._start_mobile(state["origin"])
                    if phase == "listening_mobile_access_unverified":
                        service = getattr(self.app.state, "mobile_service", None)
                        phase = "ready" if service and service.running else "error"
                        if phase == "ready" and self.reachability is None:
                            self.reachability = PublicReachability(service.config.mobile.public_origin,
                                                                  service.identity.computer_id)
                    if phase not in MESSAGES:
                        raise ValueError("unexpected connection phase")
                    self.phase, self.action = phase, provider_action(state.get("action_url", ""))
                try:
                    await asyncio.wait_for(self.stop.wait(), timeout=0.25)
                except asyncio.TimeoutError:
                    pass
        except (OSError, RuntimeError, ValueError, KeyError, TypeError):
            self.phase, self.action = "error", ""
        finally:
            if self.reachability is not None:
                await self.reachability.close()
                self.reachability = None
            await self._stop_child()
            self.app.state.mobile_service = None
            if self.context is not None:
                context, self.context = self.context, None
                await context.__aexit__(None, None, None)
            self.bridge.unlink(missing_ok=True)

    async def _start_mobile(self, origin):
        from trade_compass_agent.mobile.server import mobile_listener
        if not isinstance(origin, str) or not re.fullmatch(r"https://[a-z0-9-]+\.[a-z0-9.-]+\.ts\.net", origin):
            raise ValueError("invalid public origin")
        host = urlsplit(origin).hostname
        certs = self.directory / "tsnet" / "certs"
        config = replace(self.config, mobile=replace(self.config.mobile, enabled=True,
            host="127.0.0.1", port=0, public_origin=origin, tls_relay=True,
            tls_certfile=str(certs / (host + ".crt")), tls_keyfile=str(certs / (host + ".key"))))
        context = mobile_listener(config)
        service = await context.__aenter__()
        self.context = context
        self.app.state.mobile_service = service
        atomic_write(self.bridge, json.dumps({"origin": origin, "upstream": f"https://127.0.0.1:{service.port}"}))

    async def _stop_child(self):
        if self.process is None or self.process.returncode is not None:
            return
        self.process.stdin.close()
        try:
            await asyncio.wait_for(self.process.wait(), timeout=8)
        except asyncio.TimeoutError:
            self.process.kill()
            await self.process.wait()

    async def close(self):
        self.stop.set()
        if self.task is not None:
            await self.task
        else:
            await self._stop_child()
        self.phase, self.action = "stopped", ""

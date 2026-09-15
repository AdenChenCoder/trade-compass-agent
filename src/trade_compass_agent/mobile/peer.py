"""LAN DataChannel transport. Signalling is exchanged locally, never hosted.

All business requests enter the *same* mobile ASGI application used by TLS.
DTLS authenticates the fingerprints in the out-of-band offer/answer. Device
approval is still required; a connected transport is not a device permission.
"""
from __future__ import annotations

import asyncio
import json
import re
import secrets
import time
from dataclasses import dataclass, field

import httpx
from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription

PROTOCOL = "compass-rtc-v1"
MAX_REQUEST = 64 * 1024
MAX_RESPONSE = 8 * 1024 * 1024
# No URL forwarding, cookies, headers, browser routes, or desktop administration.
ROUTES = {
    "GET": r"(?:info|sessions|sessions/[^/?]+/messages|notifications|pairing/status|turns/[\w-]+|push)",
    "POST": r"(?:pairing/claim|pairing/verify|pairing/forget|sessions|turns|push/(?:subscription|unsubscribe|test|received|tasks))",
}


@dataclass
class Peer:
    pc: RTCPeerConnection
    channel: object
    expires_at: float
    device_id: str | None = None
    tasks: set = field(default_factory=set)
    expiry: asyncio.Task | None = None


class PeerConnections:
    def __init__(self, app, devices, identity):
        self.app, self.devices, self.identity = app, devices, identity
        self.peers: dict[str, Peer] = {}
        self.lock = asyncio.Lock()
        self.closed = False

    async def offer(self):
        async with self.lock:
            if self.closed or len(self.peers) >= 4:
                raise ValueError("连接数量已达上限，请关闭不用的手机连接后重试")
            # Explicitly empty: aiortc's default would contact a public STUN service.
            pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
            channel = pc.createDataChannel(PROTOCOL, ordered=True)
            peer_id = secrets.token_hex(16)
            invitation = self.devices.create_invitation()
            peer = Peer(pc, channel, invitation["expires_at"])
            self.peers[peer_id] = peer

            @channel.on("message")
            def message(raw):
                if len(peer.tasks) >= 8 or not isinstance(raw, str) or len(raw.encode()) > MAX_REQUEST:
                    asyncio.create_task(self.drop(peer_id))
                    return
                task = asyncio.create_task(self.dispatch(peer, raw))
                peer.tasks.add(task)
                task.add_done_callback(peer.tasks.discard)

            @pc.on("connectionstatechange")
            async def state_change():
                if pc.connectionState in {"failed", "closed"}:
                    await self.drop(peer_id)

            async def expire():
                await asyncio.sleep(max(0, peer.expires_at - time.time()))
                approved = any(d["device_id"] == peer.device_id and d["status"] == "approved"
                               for d in self.devices.list_devices())
                if not approved:
                    await self.drop(peer_id)
            peer.expiry = asyncio.create_task(expire())
            try:
                async with asyncio.timeout(15):
                    await pc.setLocalDescription(await pc.createOffer())
                return {"protocol": PROTOCOL, "peer_id": peer_id, "sdp": pc.localDescription.sdp,
                        "computer_id": self.identity.computer_id, **invitation}
            except BaseException:
                await self.drop(peer_id)
                raise

    async def answer(self, peer_id: str, sdp: str):
        peer = self.peers.get(peer_id)
        if not peer or time.time() >= peer.expires_at:
            raise ValueError("连接信息已过期，请重新生成")
        if peer.pc.signalingState != "have-local-offer":
            raise ValueError("手机返回信息已经使用，请勿重复导入")
        # This transport is data-only. Do not accept audio/video capture or media.
        if not sdp or len(sdp.encode()) > MAX_REQUEST or any(
                line.startswith("m=") and not line.startswith("m=application ") for line in sdp.splitlines()):
            raise ValueError("手机返回信息无效")
        try:
            async with asyncio.timeout(15):
                await peer.pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type="answer"))
        except Exception as exc:
            await self.drop(peer_id)
            raise ValueError("无法使用手机返回信息，请重新生成连接") from exc

    async def dispatch(self, peer, raw):
        request_id = None
        try:
            message = json.loads(raw)
            request_id = message.get("id")
            if not isinstance(request_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", request_id):
                raise ValueError()
            method, path = message.get("method"), message.get("path")
            if not isinstance(path, str) or method not in ROUTES or len(path) > 4096:
                raise ValueError()
            route = path.split("?", 1)[0].removeprefix("/mobile/v1/")
            if not path.startswith("/mobile/v1/") or not re.fullmatch(ROUTES[method], route) or ".." in path:
                result = {"status": 404, "data": {"detail": "Mobile route not found"}}
            else:
                credential = message.get("credential", "")
                if not isinstance(credential, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", credential):
                    raise ValueError()
                body = message.get("body")
                if body is not None and not isinstance(body, str):
                    raise ValueError()
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app, raise_app_exceptions=False),
                                             base_url="https://mobile.internal") as client:
                    response = await client.request(method, path, content=body,
                        headers={"Authorization": f"Bearer {credential}", "Content-Type": "application/json"})
                result = {"status": response.status_code, "data": response.json()}
                device = self.devices.lookup(credential)
                if device and device["status"] in {"pending", "approved"}:
                    peer.device_id = device["device_id"]
            text = json.dumps(result, ensure_ascii=True, separators=(",", ":"))
            if len(text) > MAX_RESPONSE:
                text = json.dumps({"status": 413, "data": {"detail": "内容过大，请在电脑查看"}})
            # ASCII JSON chunks stay below SCTP message limits even for CJK text.
            for offset in range(0, len(text), 16000):
                async with asyncio.timeout(10):
                    while peer.channel.bufferedAmount > 256 * 1024:
                        await asyncio.sleep(0.02)
                if peer.channel.readyState != "open":
                    return
                peer.channel.send(json.dumps({"id": request_id, "chunk": text[offset:offset + 16000],
                                               "end": offset + 16000 >= len(text)}))
        except (ValueError, TypeError, AttributeError, KeyError):
            if peer.channel.readyState == "open":
                peer.channel.send(json.dumps({"id": request_id, "chunk": json.dumps(
                    {"status": 400, "data": {"detail": "Invalid mobile request"}}), "end": True}))
        except Exception:
            # No device secret / message body is logged. Caller retains request ID
            # and checks its durable receipt; a failed transport never reruns Agent.
            peer.channel.close()

    async def drop(self, peer_id):
        peer = self.peers.pop(peer_id, None)
        if peer:
            current = asyncio.current_task()
            tasks = [task for task in [peer.expiry, *peer.tasks] if task is not None and task is not current]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await peer.pc.close()

    async def revoke(self, device_id):
        for key, peer in list(self.peers.items()):
            if peer.device_id == device_id:
                await self.drop(key)

    async def close(self):
        async with self.lock:
            self.closed = True
            for key in list(self.peers):
                await self.drop(key)

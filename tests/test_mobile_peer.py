"""Consumer tests use an actual DTLS/SCTP connection, never a mocked transport."""
import asyncio
import json
import secrets

from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription
import pytest

from trade_compass_agent.config import AppConfig
from trade_compass_agent.mobile.api import create_mobile_app
from trade_compass_agent.mobile.identity import load_or_create_identity
from trade_compass_agent.mobile.pairing import DeviceStore
from trade_compass_agent.mobile.peer import PeerConnections, PROTOCOL
from trade_compass_agent.runtime.session import SessionMessageRecord, SessionStore


class Phone:
    async def connect(self, peers):
        self.pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        self.channel = None
        self.responses = asyncio.Queue()
        self.text = ""
        self.secret = secrets.token_urlsafe(32)

        @self.pc.on("datachannel")
        def datachannel(channel):
            self.channel = channel

            @channel.on("message")
            def receive(raw):
                chunk = json.loads(raw)
                self.text += chunk['chunk']
                if chunk['end']:
                    self.responses.put_nowait(json.loads(self.text))
                    self.text = ""
        self.offer = await peers.offer()
        assert self.offer['protocol'] == PROTOCOL
        await self.pc.setRemoteDescription(RTCSessionDescription(sdp=self.offer['sdp'], type='offer'))
        await self.pc.setLocalDescription(await self.pc.createAnswer())
        await peers.answer(self.offer['peer_id'], self.pc.localDescription.sdp)
        async with asyncio.timeout(10):
            while not self.channel or self.channel.readyState != 'open':
                await asyncio.sleep(.02)
        return self

    async def request(self, path, method='GET', body=None):
        self.channel.send(json.dumps({'id': secrets.token_hex(10), 'credential': self.secret,
                                      'path': path, 'method': method,
                                      'body': json.dumps(body) if body is not None else None}))
        return await asyncio.wait_for(self.responses.get(), 10)

    async def claim(self):
        return await self.request('/mobile/v1/pairing/claim', 'POST', {
            'invitation': self.offer['invitation'], 'device_secret': self.secret, 'name': '真实直连测试'})


@pytest.fixture
def peer_app(tmp_path, monkeypatch):
    config = AppConfig(data_dir=tmp_path / 'data', memory_dir=tmp_path / 'memory')
    monkeypatch.setenv('TRADE_COMPASS_DATA_DIR', str(config.data_dir))
    monkeypatch.setenv('TRADE_COMPASS_MEMORY_DIR', str(config.memory_dir))
    identity = load_or_create_identity(config.data_dir / 'mobile')
    devices = DeviceStore(config.data_dir / 'mobile')
    app = create_mobile_app(config, devices, identity)
    return config, identity, devices, app


def test_real_peer_approval_chunks_reconnect_and_revoke(peer_app):
    async def run():
        config, identity, devices, app = peer_app
        peers = PeerConnections(app, devices, identity)
        phone = await Phone().connect(peers)
        try:
            assert (await phone.request('/mobile/v1/sessions'))['status'] == 401
            result = await phone.claim()
            assert result['status'] == 202
            device = result['data']
            assert (await phone.request('/mobile/v1/sessions'))['status'] == 403
            assert (await phone.request('/mobile/v1/sessions', 'POST'))['status'] == 403
            assert 'verification_code' not in device
            code = next(d['verification_code'] for d in devices.list_devices() if d['device_id'] == device['device_id'])
            assert (await phone.request('/mobile/v1/pairing/verify', 'POST', {'verification_code': code}))['status'] == 200
            store = SessionStore(config.data_dir / 'agent_sessions')
            created = await phone.request('/mobile/v1/sessions', 'POST')
            assert created['status'] == 200
            assert store.load(created['data']['session_id']) is not None
            session = store.get_or_create('shared-rtc')
            content = '原始会话' * 12000
            store.append(session, SessionMessageRecord(role='user', content=content))
            result = await phone.request('/mobile/v1/sessions/shared-rtc/messages')
            assert result['status'] == 200
            assert result['data']['messages'][0]['content'] == content
            for path in ['/api/config', '/mobile/v1/../api/config', '/mobile/v1/browser/connection',
                         '/mobile/v1/pairing/status/../../api/config', 'https://example.org/mobile/v1/info']:
                assert (await phone.request(path))['status'] == 404
            secret = phone.secret
            await phone.pc.close()
            await peers.close()
            # New transport, same durable identity/permissions and original session.
            devices = DeviceStore(config.data_dir / 'mobile')
            peers = PeerConnections(app, devices, identity)
            phone = await Phone().connect(peers)
            phone.secret = secret
            assert (await phone.request('/mobile/v1/pairing/status'))['data']['status'] == 'approved'
            assert (await phone.request('/mobile/v1/sessions/shared-rtc/messages'))['data']['messages'][0]['content'] == content
            devices.revoke(device['device_id'])
            assert (await phone.request('/mobile/v1/sessions'))['status'] == 401
            await peers.revoke(device['device_id'])
            assert not peers.peers
        finally:
            await phone.pc.close()
            await peers.close()
    asyncio.run(run())


def test_peer_answer_is_single_use_and_close_releases_resources(peer_app):
    async def run():
        _, identity, devices, app = peer_app
        peers = PeerConnections(app, devices, identity)
        phone = await Phone().connect(peers)
        try:
            with pytest.raises(ValueError, match='已经使用'):
                await peers.answer(phone.offer['peer_id'], phone.pc.localDescription.sdp)
            await peers.close()
            assert not peers.peers
            with pytest.raises(ValueError):
                await peers.offer()
        finally:
            await phone.pc.close()
            await peers.close()
    asyncio.run(run())

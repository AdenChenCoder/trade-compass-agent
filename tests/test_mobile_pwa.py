from dataclasses import replace
from datetime import datetime, timedelta, timezone
import base64
import json
import secrets
from types import SimpleNamespace

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import hashes
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient
import http_ece
import pytest
import requests

from trade_compass_agent.config import AppConfig, MobileConfig
from trade_compass_agent.mobile.api import COOKIE, create_mobile_app
from trade_compass_agent.mobile.identity import load_or_create_identity
from trade_compass_agent.mobile.pairing import DeviceStore
from trade_compass_agent.mobile.push import PushStore, validate_subscription
from trade_compass_agent.mobile.tls import browser_tls
from trade_compass_agent.runtime.session import SessionStore, SessionMessageRecord

ORIGIN = 'https://computer.example:19705'
HEADERS = {'Origin': ORIGIN, 'X-Compass-PWA': '1'}


@pytest.fixture
def pwa(tmp_path, monkeypatch):
    config = AppConfig(data_dir=tmp_path / 'data', memory_dir=tmp_path / 'memory',
                       mobile=MobileConfig(True, '127.0.0.1', 19705, ORIGIN))
    monkeypatch.setenv('TRADE_COMPASS_DATA_DIR', str(config.data_dir))
    monkeypatch.setenv('TRADE_COMPASS_MEMORY_DIR', str(config.memory_dir))
    identity = load_or_create_identity(config.data_dir / 'mobile')
    devices = DeviceStore(config.data_dir / 'mobile')
    app = create_mobile_app(config, devices, identity)
    browser = TestClient(app, base_url=ORIGIN)
    from trade_compass_agent.web.app import create_app
    local_app = create_app()
    local_app.state.mobile_service = SimpleNamespace(running=True, config=config, identity=identity,
        devices=devices, port=19705, push=app.state.web_push)
    local = TestClient(local_app, client=('127.0.0.1', 1000))
    return config, identity, devices, browser, local


def pair_browser(pwa):
    _, _, _, browser, local = pwa
    invite = local.post('/api/mobile/pairing/invitations').json()
    result = browser.post('/mobile/v1/browser/claim', headers=HEADERS,
                          json={'invitation': invite['invitation'], 'name': 'iPhone PWA'})
    assert result.status_code == 202
    assert 'verification_code' not in result.json()
    device = next(d for d in local.get('/api/mobile/devices').json()['devices'] if d['device_id'] == result.json()['device_id'])
    assert local.post(f"/api/mobile/devices/{device['device_id']}/approve",
                      json={'verification_code': device['verification_code']}).status_code == 200
    return device


def subscription():
    key = ec.generate_private_key(ec.SECP256R1())
    auth = secrets.token_bytes(16)
    def encode(data):
        return base64.urlsafe_b64encode(data).rstrip(b'=').decode()
    data = {'endpoint': 'https://web.push.apple.com/Q/test-device', 'keys': {
        'p256dh': encode(key.public_key().public_bytes(serialization.Encoding.X962,
                                                     serialization.PublicFormat.UncompressedPoint)),
        'auth': encode(auth)}}
    return key, auth, data


def test_browser_pairing_cookie_history_restart_and_revoke(pwa):
    config, identity, devices, browser, local = pwa
    assert browser.get('/phone/').status_code == 200
    manifest = browser.get('/phone/manifest.webmanifest').json()
    assert manifest['display'] == 'standalone'
    assert manifest['start_url'] == './'
    assert browser.get('/phone/sw.js').headers['cache-control'] == 'no-store'
    assert local.get('/api/mobile/status').json()['pwa_url'] == ORIGIN + '/mobile/'
    assert browser.get('/mobile/').content == browser.get('/phone/').content
    assert browser.get('/mobile/manifest.webmanifest').json() == manifest
    assert browser.get('/mobile/sw.js').headers['cache-control'] == 'no-store'
    assert browser.get('/mobile/', headers={'Host': 'evil.example'}).status_code == 403
    assert browser.get('/mobile/v1/push').status_code == 401
    assert browser.get('/api/config').status_code == 404
    invite = local.post('/api/mobile/pairing/invitations').json()
    response = browser.post('/mobile/v1/browser/claim', headers=HEADERS,
                            json={'invitation': invite['invitation'], 'name': 'iPhone'})
    cookie = response.headers['set-cookie']
    assert all(flag in cookie for flag in ('HttpOnly', 'Secure', 'SameSite=strict', 'Path=/'))
    secret = browser.cookies.get(COOKIE)
    assert secret not in response.text
    assert browser.get('/mobile/v1/sessions').status_code == 403
    assert 'verification_code' not in response.json()
    device = next(d for d in local.get('/api/mobile/devices').json()['devices'] if d['device_id'] == response.json()['device_id'])
    local.post(f"/api/mobile/devices/{device['device_id']}/approve",
               json={'verification_code': device['verification_code']})
    session = SessionStore(config.data_dir / 'agent_sessions').create()
    SessionStore(config.data_dir / 'agent_sessions').append(session, SessionMessageRecord(role='user', content='同一份原始会话'))
    endpoint = f'/agent/sessions/{session.session_id}/messages'
    assert browser.get('/mobile/v1/sessions/' + session.session_id + '/messages').json() == local.get('/api' + endpoint).json()
    restored = TestClient(create_mobile_app(config, DeviceStore(config.data_dir / 'mobile'), identity), base_url=ORIGIN)
    restored.cookies.update(browser.cookies)
    assert restored.get('/mobile/v1/browser/connection').json()['connected'] is True
    assert restored.get('/mobile/v1/sessions').status_code == 200
    assert restored.get('/mobile/v1/push').status_code == 200
    local.delete(f"/api/mobile/devices/{device['device_id']}")
    assert restored.get('/mobile/v1/sessions').status_code == 401
    assert secret.encode() not in devices.path.read_bytes()


def test_cookie_csrf_host_and_unapproved_push_boundaries(pwa):
    _, _, devices, browser, _ = pwa
    invite = devices.create_invitation()
    body = {'invitation': invite['invitation'], 'name': 'Browser'}
    assert browser.post('/mobile/v1/browser/claim', json=body).status_code == 403
    assert browser.post('/mobile/v1/browser/claim', json=body,
                        headers={**HEADERS, 'Origin': 'https://evil.example'}).status_code == 403
    assert browser.post('/mobile/v1/browser/claim', json=body,
                        headers={**HEADERS, 'Host': 'evil.example'}).status_code == 403
    assert browser.post('/mobile/v1/browser/claim', json=body, headers=HEADERS).status_code == 202
    assert browser.post('/mobile/v1/push/test', headers=HEADERS).status_code == 403
    assert browser.post('/mobile/v1/push/tasks', headers=HEADERS, json={'enabled': True}).status_code == 403
    assert browser.post('/mobile/v1/browser/forget').status_code == 403
    assert browser.get('/mobile/v1/sessions', headers={'Host': 'evil.example'}).status_code == 403
    assert browser.get('/mobile/v1/sessions', headers={'Sec-Fetch-Site': 'cross-site'}).status_code == 403
    assert browser.post('/mobile/v1/browser/forget', headers=HEADERS).status_code == 200
    assert browser.get('/mobile/v1/browser/connection').json()['connected'] is False


def test_task_reminders_need_explicit_authorized_opt_in(pwa):
    _, _, _, browser, local = pwa
    pair_browser(pwa)
    assert browser.post('/mobile/v1/push/tasks', headers=HEADERS, json={'enabled': True}).status_code == 409
    browser.post('/mobile/v1/push/subscription', headers=HEADERS, json=subscription()[2])
    assert browser.get('/mobile/v1/push').json()['tasks_enabled'] is False
    assert browser.post('/mobile/v1/push/tasks', json={'enabled': True}).status_code == 403
    assert browser.post('/mobile/v1/push/tasks', headers=HEADERS, json={'enabled': True}).json()['tasks_enabled']
    assert local.get('/api/mobile/devices').json()['devices'][0]['push']['tasks_enabled'] is True
    assert browser.post('/mobile/v1/push/tasks', headers=HEADERS, json={'enabled': False}).json()['tasks_enabled'] is False
    assert browser.get('/mobile/v1/push').json()['subscribed'] is True
    browser.post('/mobile/v1/push/tasks', headers=HEADERS, json={'enabled': True})
    browser.post('/mobile/v1/push/unsubscribe', headers=HEADERS)
    assert browser.get('/mobile/v1/push').json()['tasks_enabled'] is False


def test_real_webpush_encryption_acceptance_ack_and_revocation(pwa, monkeypatch):
    config, _, _, browser, local = pwa
    device = pair_browser(pwa)
    private, auth, data = subscription()
    assert browser.post('/mobile/v1/push/subscription', json=data, headers=HEADERS).status_code == 200
    sent = []
    def post(_session, url, **kwargs):
        assert url == data['endpoint']
        assert kwargs['allow_redirects'] is False
        assert kwargs['timeout'] == 10
        assert kwargs['headers']['Authorization'].startswith('vapid ')
        assert kwargs['headers']['Content-Encoding'] == 'aes128gcm'
        payload = http_ece.decrypt(kwargs['data'], private_key=private, auth_secret=auth, version='aes128gcm')
        sent.append(json.loads(payload))
        response = requests.Response()
        response.status_code = 201
        return response
    monkeypatch.setattr(requests.Session, 'post', post)
    endpoint = f"/api/mobile/devices/{device['device_id']}/push/test"
    response = local.post(endpoint)
    assert response.json()['status'] == 'accepted'
    assert len(sent) == 1
    assert sent[0]['title'] == '交易罗盘连接测试'
    state = browser.get('/mobile/v1/push').json()
    assert state['last_test']['received_at'] is None
    assert browser.post('/mobile/v1/push/received', json={'id': sent[0]['id']}, headers=HEADERS).status_code == 200
    assert browser.get('/mobile/v1/push').json()['last_test']['received_at'] is not None
    assert local.post(endpoint).status_code == 429
    restored = PushStore(config.data_dir / 'mobile', ORIGIN)
    assert restored.public_key == state['public_key']
    assert restored.status(device['device_id'])['subscribed'] is True
    assert restored.path.stat().st_mode & 0o777 == 0o600
    assert restored.key_path.stat().st_mode & 0o777 == 0o600
    other = config.data_dir / 'other-computer'
    other.mkdir()
    assert restored.public_key != PushStore(other, ORIGIN).public_key
    local.delete(f"/api/mobile/devices/{device['device_id']}")
    assert local.post(endpoint).status_code == 403
    assert browser.post('/mobile/v1/push/test', headers=HEADERS).status_code == 401
    assert len(sent) == 1


@pytest.mark.parametrize('endpoint', ['http://web.push.apple.com/x', 'https://127.0.0.1/x',
    'https://web.push.apple.com.evil.com/x', 'https://web.push.apple.com:8443/x',
    'https://user:password@web.push.apple.com/x', 'https://web.push.apple.com/x#fragment'])
def test_push_subscription_rejects_nonvendor_targets(endpoint):
    from fastapi import HTTPException
    _, _, data = subscription()
    with pytest.raises(HTTPException):
        validate_subscription({**data, 'endpoint': endpoint})


def test_push_expired_subscription_is_removed_without_leaking_provider_response(pwa, monkeypatch):
    _, _, _, browser, local = pwa
    device = pair_browser(pwa)
    _, _, data = subscription()
    browser.post('/mobile/v1/push/subscription', json=data, headers=HEADERS)
    def post(*args, **kwargs):
        response = requests.Response()
        response.status_code = 410
        response._content = b'private vendor diagnostic'
        return response
    monkeypatch.setattr(requests.Session, 'post', post)
    response = local.post(f"/api/mobile/devices/{device['device_id']}/push/test")
    assert response.json()['status'] == 'expired'
    assert 'private' not in response.text
    assert browser.get('/mobile/v1/push').json()['subscribed'] is False


def test_external_tls_requires_complete_matching_valid_configuration(tmp_path):
    identity = load_or_create_identity(tmp_path / 'mobile')
    private = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'computer.example')])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(private.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1)).add_extension(x509.SubjectAlternativeName([
            x509.DNSName('computer.example')]), critical=False).sign(private, hashes.SHA256()))
    certfile, keyfile = tmp_path / 'cert.pem', tmp_path / 'key.pem'
    certfile.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    keyfile.write_bytes(private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                             serialization.NoEncryption()))
    config = MobileConfig(True, '127.0.0.1', 19705, ORIGIN, str(certfile), str(keyfile))
    current, _ = browser_tls(config, identity)
    assert current.computer_id == identity.computer_id
    assert current.certificate_sha256 != identity.certificate_sha256
    for invalid in [replace(config, tls_keyfile=''), replace(config, public_origin='http://computer.example:19705'),
                    replace(config, public_origin='https://other.example:19705'),
                    replace(config, public_origin='https://computer.example:443')]:
        with pytest.raises(ValueError):
            browser_tls(invalid, identity)
    relay = replace(config, public_origin='https://computer.example', tls_relay=True)
    relayed, _ = browser_tls(relay, identity)
    assert relayed.computer_id == identity.computer_id
    assert relayed.certificate_sha256 == current.certificate_sha256
    for invalid in [replace(relay, host='0.0.0.0'), replace(relay, host='192.0.2.1'),
                    replace(relay, tls_keyfile=''), replace(relay, public_origin='https://other.example'),
                    replace(relay, public_origin='http://computer.example'),
                    replace(relay, public_origin='https://computer.example:8443'),
                    MobileConfig(tls_relay=True, host='127.0.0.1')]:
        with pytest.raises(ValueError):
            browser_tls(invalid, identity)


def claim_for_code(pwa):
    _, _, _, browser, local = pwa
    invite = local.post('/api/mobile/pairing/invitations').json()
    assert 'verification_code' not in invite
    result = browser.post('/mobile/v1/browser/claim', headers=HEADERS,
                          json={'invitation': invite['invitation'], 'name': '我的手机'})
    assert result.status_code == 202
    assert 'verification_code' not in result.json()
    phone_status = browser.get('/mobile/v1/pairing/status').json()
    assert 'verification_code' not in phone_status
    assert phone_status['status'] == 'pending'
    device = next(d for d in local.get('/api/mobile/devices').json()['devices']
                  if d['device_id'] == result.json()['device_id'])
    return device


def test_phone_enters_private_desktop_code_and_continues_original_history(pwa):
    config, _, _, browser, local = pwa
    device = claim_for_code(pwa)
    code = device['verification_code']
    secret = browser.cookies.get(COOKIE)
    store = SessionStore(config.data_dir / 'agent_sessions')
    session = store.create()
    store.append(session, SessionMessageRecord(role='user', content='配对前的原始会话'))
    body = {'verification_code': code}
    assert browser.get('/mobile/v1/sessions').status_code == 403
    assert browser.post('/mobile/v1/pairing/verify', json=body).status_code == 403
    assert browser.post('/mobile/v1/pairing/verify', headers={**HEADERS, 'Origin': 'https://evil.example'}, json=body).status_code == 403
    assert browser.post('/mobile/v1/pairing/verify', headers=HEADERS, json={**body, 'device_id': 'another-phone'}).status_code == 422
    wrong = f'{(int(code) + 1) % 1_000_000:06d}'
    assert browser.post('/mobile/v1/pairing/verify', headers=HEADERS, json={'verification_code': wrong}).status_code == 409
    assert browser.get('/mobile/v1/sessions').status_code == 403
    assert browser.post('/mobile/v1/pairing/verify', headers=HEADERS, json=body).json() == {'status': 'approved'}
    assert browser.post('/mobile/v1/pairing/verify', headers=HEADERS, json=body).status_code == 200
    assert 'verification_code' not in browser.get('/mobile/v1/pairing/status').json()
    assert browser.cookies.get(COOKIE) == secret
    path = f'/sessions/{session.session_id}/messages'
    assert browser.get('/mobile/v1' + path).json() == local.get('/api/agent' + path).json()
    assert local.get('/api/mobile/devices').json()['devices'][0]['status'] == 'approved'
    local.delete(f"/api/mobile/devices/{device['device_id']}")
    assert browser.post('/mobile/v1/pairing/verify', headers=HEADERS, json=body).status_code == 401


def test_wrong_code_limit_survives_restart_and_cannot_be_reset_by_refresh(pwa):
    config, identity, _, browser, local = pwa
    device = claim_for_code(pwa)
    wrong = f"{(int(device['verification_code']) + 1) % 1_000_000:06d}"
    for remaining in [4, 3]:
        response = browser.post('/mobile/v1/pairing/verify', headers=HEADERS, json={'verification_code': wrong})
        assert response.status_code == 409 and str(remaining) in response.json()['detail']
    restored = TestClient(create_mobile_app(config, DeviceStore(config.data_dir / 'mobile'), identity), base_url=ORIGIN)
    restored.cookies.update(browser.cookies)
    for remaining in [2, 1]:
        assert restored.get('/mobile/v1/pairing/status').json()['status'] == 'pending'
        response = restored.post('/mobile/v1/pairing/verify', headers=HEADERS, json={'verification_code': wrong})
        assert response.status_code == 409 and str(remaining) in response.json()['detail']
    assert '次数过多' in restored.post('/mobile/v1/pairing/verify', headers=HEADERS, json={'verification_code': wrong}).json()['detail']
    assert restored.post('/mobile/v1/pairing/verify', headers=HEADERS, json={'verification_code': device['verification_code']}).status_code == 401
    assert restored.get('/mobile/v1/sessions').status_code == 401
    assert local.get('/api/mobile/devices').json()['devices'][0]['status'] == 'revoked'


def test_expired_and_legacy_pending_codes_cannot_self_approve(pwa, monkeypatch):
    import sqlite3
    config, _, devices, browser, local = pwa
    device = claim_for_code(pwa)
    with sqlite3.connect(devices.path) as conn:
        # Emulate a pre-upgrade pending row, whose code was sent to the phone.
        conn.execute('DELETE FROM pairing_attempts WHERE device_id = ?', (device['device_id'],))
    restored = DeviceStore(config.data_dir / 'mobile')
    response = browser.post('/mobile/v1/pairing/verify', headers=HEADERS, json={'verification_code': device['verification_code']})
    assert response.status_code == 409
    assert restored.list_devices()[0]['status'] == 'pending'
    # The original desktop-only approval contract remains available for old clients.
    assert local.post(f"/api/mobile/devices/{device['device_id']}/approve", json={'verification_code': device['verification_code']}).status_code == 200
    assert browser.get('/mobile/v1/sessions').status_code == 200
    browser.post('/mobile/v1/browser/forget', headers=HEADERS)
    next_device = claim_for_code(pwa)
    monkeypatch.setattr('trade_compass_agent.mobile.pairing.time.time', lambda: next_device['expires_at'] + 1)
    assert browser.post('/mobile/v1/pairing/verify', headers=HEADERS, json={'verification_code': next_device['verification_code']}).status_code == 401


def test_concurrent_guesses_do_not_bypass_attempt_limit(pwa):
    from concurrent.futures import ThreadPoolExecutor
    from trade_compass_agent.mobile.pairing import PairingError
    _, _, devices, _, _ = pwa
    device = claim_for_code(pwa)
    wrong = f"{(int(device['verification_code']) + 1) % 1_000_000:06d}"
    def guess(_):
        with pytest.raises(PairingError):
            devices.verify(device['device_id'], wrong)
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(guess, range(8)))
    assert devices.list_devices()[0]['status'] == 'revoked'
    with pytest.raises(PairingError):
        devices.verify(device['device_id'], device['verification_code'])


def test_browser_creation_requires_same_origin_and_approved_device(pwa):
    config, _, _, browser, local = pwa
    assert browser.post('/mobile/v1/sessions', headers=HEADERS).status_code == 401
    pair_browser(pwa)
    assert browser.post('/mobile/v1/sessions').status_code == 403
    assert browser.post('/mobile/v1/sessions', headers={**HEADERS, 'Origin': 'https://evil.example'}).status_code == 403
    assert not list((config.data_dir / 'agent_sessions').glob('*.jsonl'))
    created = browser.post('/mobile/v1/sessions', headers=HEADERS)
    assert created.status_code == 200
    sid = created.json()['session_id']
    assert local.get(f'/api/agent/sessions/{sid}/messages').json()['messages'] == []

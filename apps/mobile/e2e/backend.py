"""Finite Playwright fixture: private data, no scheduler and a deterministic agent."""
# Environment must be isolated before importing application modules.
# ruff: noqa: E402
import os
from pathlib import Path
import tempfile
import time

root = Path(tempfile.mkdtemp(prefix="compass-ui-test-"))
config = root / "config.yaml"
config.write_text("scheduler:\n  enabled: false\nmobile:\n  enabled: false\n  host: 127.0.0.1\n  port: 19745\n")
# PWA integration mode uses only an ephemeral certificate and synthetic test push recipient.
peer_mode = os.environ.get("COMPASS_PEER_E2E") == "1"
if peer_mode:
    config.write_text("scheduler:\n  enabled: false\nmobile:\n  enabled: true\n  host: 127.0.0.1\n  port: 19750\n")
pwa_mode = os.environ.get("COMPASS_PWA_E2E") == "1"
if pwa_mode or peer_mode:
    import base64
    import hashlib
    import secrets
    import json
    from datetime import datetime, timedelta, timezone
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    private = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(private.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1)).add_extension(x509.SubjectAlternativeName([
            x509.DNSName("localhost")]), critical=False).sign(private, hashes.SHA256()))
    certfile, keyfile = root / "cert.pem", root / "key.pem"
    certfile.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    keyfile.write_bytes(private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                             serialization.NoEncryption()))
    keyfile.chmod(0o600)
    if pwa_mode:
        config.write_text(f"scheduler:\n  enabled: false\nmobile:\n  enabled: true\n  host: 127.0.0.1\n  port: 19746\n  public_origin: https://localhost:19746\n  tls_certfile: {certfile}\n  tls_keyfile: {keyfile}\n")
    spki = base64.b64encode(hashlib.sha256(private.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)).digest()).decode()
    receiver = ec.generate_private_key(ec.SECP256R1())
    auth = secrets.token_bytes(16)
    def encode(value):
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode()
    subscription = {"endpoint": "https://web.push.apple.com/Q/browser-fixture", "keys": {
        "p256dh": encode(receiver.public_key().public_bytes(serialization.Encoding.X962,
                                                           serialization.PublicFormat.UncompressedPoint)),
        "auth": encode(auth)}}
    import requests
    import http_ece
    payloads = []
    def fake_vendor_post(_session, url, **kwargs):
        assert url == subscription["endpoint"]
        assert kwargs["allow_redirects"] is False
        payloads.append(json.loads(http_ece.decrypt(kwargs["data"], private_key=receiver,
                                                   auth_secret=auth, version="aes128gcm")))
        response = requests.Response()
        response.status_code = 201
        return response
    requests.Session.post = fake_vendor_post
os.environ.update(TRADE_COMPASS_HOME=str(root), TRADE_COMPASS_CONFIG=str(config),
                  TRADE_COMPASS_DATA_DIR=str(root / "data"), TRADE_COMPASS_MEMORY_DIR=str(root / "memory"),
                  TRADE_COMPASS_NO_SCHEDULER="true", TRADE_COMPASS_DATA_PROVIDER="sample")
from trade_compass_agent.web.app import create_app
from trade_compass_agent.web import agent_api
from trade_compass_agent.runtime.session import SessionStore, SessionMessageRecord
from trade_compass_agent.runtime.types import TurnResponse
from trade_compass_agent.ops.notifications import JsonNotificationStore
from trade_compass_agent.domain import Notification
from starlette.routing import Mount
from starlette.staticfiles import StaticFiles
import uvicorn

store = SessionStore(root / "data/agent_sessions")
session = store.get_or_create("shared-demo")
store.set_title(session, "消费行业观察")
store.append(session, SessionMessageRecord(role="user", content="帮我整理最近关注的消费行业问题。"))
store.append(session, SessionMessageRecord(role="assistant", content="我们可以从需求变化、企业盈利和估值三个方面继续讨论。"))
JsonNotificationStore(root / "data/notifications.jsonl").append(Notification(channel="scheduler:demo", title="收盘复盘已完成", message="今天的观察清单已经整理好。\n\n- 关注成交量变化\n- 对照原来的研究假设"))

class Loop:
    def run_turn(self, message, **kwargs):
        current = store.load(kwargs["session_id"])
        store.append(current, SessionMessageRecord(role="user", content=message))
        time.sleep(2 if message == "slow-test" else 0.4)
        answer = "已在电脑上的同一个会话收到：" + message
        store.append(current, SessionMessageRecord(role="assistant", content=answer))
        return TurnResponse(session_id=current.session_id, summary=answer, sections=[])
agent_api._loop_for_session = lambda *args: Loop()
# Release switching is confined to this finite test fixture and private assets.
if pwa_mode:
    import shutil
    import re
    import trade_compass_agent.mobile.server as mobile_server
    bundle = Path(__file__).parents[3] / 'src/trade_compass_agent/mobile_dist'
    releases = {'current': bundle}
    provided = os.environ.get('COMPASS_PWA_UPGRADE_FIXTURES')
    for label, version in [('legacy', '000000000001'), ('before-fix', '000000000002')]:
        directory = Path(provided) / label if provided else root / label
        if not provided:
            shutil.copytree(bundle, directory)
            current_sw = (bundle / 'sw.js').read_text()
            assets = re.search(r'const ASSETS = (.*);', current_sw).group(1)
            old_sw = (Path(__file__).parent / 'fixtures/sw-before-updates.js').read_text()
            (directory / 'sw.js').write_text(old_sw.replace('__VERSION__', version).replace('__PRECACHE__', assets).replace('__PEER_MODE__', 'false'))
        releases[label] = directory
    # A later release with exactly the same business code exercises the update UI.
    next_release = root / 'next-release'
    shutil.copytree(bundle, next_release)
    current_sw = (bundle / 'sw.js').read_text()
    match = re.search(r"const VERSION = '([a-f0-9]{12})'", current_sw)
    if match:
        version = match.group(1)
        (next_release / 'sw.js').write_text(current_sw.replace(version, 'eeeeeeeeeeee'))
        (next_release / 'index.html').write_text((bundle / 'index.html').read_text().replace(version, 'eeeeeeeeeeee'))
    releases['next'] = next_release
    incomplete = root / 'incomplete-release'
    shutil.copytree(next_release, incomplete)
    (incomplete / 'sw.js').write_text((next_release / 'sw.js').read_text().replace('eeeeeeeeeeee', 'dddddddddddd'))
    next((incomplete / 'assets').glob('*.css')).unlink()
    releases['incomplete'] = incomplete
    selected_release = ['current']
    original_mobile_app = mobile_server.create_mobile_app
    class ReleaseAssets:
        async def __call__(self, scope, receive, send):
            await StaticFiles(directory=releases[selected_release[0]], html=True)(scope, receive, send)
    def with_test_releases(*args, **kwargs):
        mobile = original_mobile_app(*args, **kwargs)
        for route in mobile.router.routes:
            if getattr(route, 'path', '') in ('/mobile', '/phone'):
                route.app = ReleaseAssets()
        return mobile
    mobile_server.create_mobile_app = with_test_releases
app = create_app()
import logging
logging.getLogger("aioice").setLevel(logging.WARNING)
if pwa_mode or peer_mode:
    from starlette.routing import Route
    from starlette.responses import JSONResponse
    async def fixture_info(request):
        return JSONResponse({"spki": spki, "subscription": subscription, "payloads": payloads})
    app.router.routes.insert(0, Route("/test/pwa", fixture_info))
    if pwa_mode:
        async def fixture_release(request):
            label = (await request.json()).get('release')
            if label not in releases:
                return JSONResponse({'error': 'unknown fixture release'}, status_code=400)
            selected_release[0] = label
            return JSONResponse({'release': label})
        app.router.routes.insert(0, Route('/test/release', fixture_release, methods=['POST']))
    async def fixture_task(request):
        from trade_compass_agent.config import load_app_config
        from trade_compass_agent.ops.run_store import SqliteRunStore
        from trade_compass_agent.ops.delivery import DeliveryRouter
        from trade_compass_agent.ops.job_definition import DeliveryConfig
        from trade_compass_agent.web.api import notifications_payload
        from dataclasses import replace
        cfg = load_app_config()
        cfg = replace(cfg, notifications=replace(cfg.notifications, macos_enabled=False))
        runs = SqliteRunStore(cfg.data_dir / 'scheduler.db')
        run = runs.create_run('mobile-resource-check', trigger='api')
        runs.start_run(run)
        assert (Path(__file__).parents[3] / 'src/trade_compass_agent/mobile_dist/index.html').is_file()
        body = await request.body()
        status = json.loads(body).get('status') if body else None
        if status == 'failed':
            runs.fail_run(run, error='任务执行失败：仅用于界面验收。')
        else:
            runs.complete_run(run, message='任务推送验证：手机页面资源检查完成。')
        DeliveryRouter(cfg).deliver(run, DeliveryConfig())
        return JSONResponse({'run_id': run.id, 'notice': notifications_payload(cfg, 1)[0].model_dump()})
    app.router.routes.insert(0, Route('/test/task', fixture_task, methods=['POST']))
else:
    app.router.routes.insert(0, Mount("/phone", app=StaticFiles(directory=Path(__file__).parents[1] / "dist", html=True)))
if peer_mode:
    import asyncio
    from starlette.applications import Starlette
    static = Starlette(routes=[Mount("/mobile", app=StaticFiles(directory=Path(__file__).parents[1] / "dist-peer", html=True))])
    async def main():
        desktop = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=19749, access_log=False, log_config=None))
        site = uvicorn.Server(uvicorn.Config(static, host="127.0.0.1", port=19748, ssl_certfile=str(certfile), ssl_keyfile=str(keyfile), access_log=False, log_config=None))
        await asyncio.gather(desktop.serve(), site.serve())
    asyncio.run(main())
else:
    uvicorn.run(app, host="127.0.0.1", port=19747 if pwa_mode else 19744, access_log=False, log_config=None)

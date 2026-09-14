"""Reload relay certificates without restarting requests or changing device identity."""
import asyncio
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import logging
from pathlib import Path
import ssl
import tempfile

from cryptography import x509
from cryptography.exceptions import UnsupportedAlgorithm

from trade_compass_agent.mobile.identity import ComputerIdentity
from trade_compass_agent.mobile.tls import browser_tls

logger = logging.getLogger(__name__)
RELOAD_SECONDS = 30


@dataclass(frozen=True)
class _Certificate:
    context: ssl.SSLContext
    identity: ComputerIdentity
    expires_at: datetime
    digest: bytes


class RelayTLS:
    def __init__(self, config, identity, directory: Path):
        self.config = config
        self.identity = identity
        self.directory = directory
        self.current = self._read()
        self.context = self.current.context
        # asyncio retains the listening context. Select a fully prepared context
        # for each handshake instead of mutating its certificate/key in place.
        self.context.sni_callback = self._select

    @property
    def valid(self):
        return datetime.now(timezone.utc) < self.current.expires_at

    def _select(self, connection, _server_name, _context):
        if not self.valid:
            return ssl.ALERT_DESCRIPTION_CERTIFICATE_EXPIRED
        connection.context = self.current.context

    def _read(self):
        cert = Path(self.config.tls_certfile).read_bytes()
        key = Path(self.config.tls_keyfile).read_bytes()
        digest = hashlib.sha256(cert + b"\n" + key).digest()
        if hasattr(self, "current") and self.current.digest == digest:
            return self.current
        # The provider can replace the two files separately. Validate and load
        # the same private snapshot; a partial update must not replace live TLS.
        with tempfile.NamedTemporaryFile(dir=self.directory, prefix=".tls-", suffix=".pem") as snapshot:
            snapshot.write(cert + b"\n" + key)
            snapshot.flush()
            config = replace(self.config, tls_certfile=snapshot.name, tls_keyfile=snapshot.name)
            identity, _ = browser_tls(config, self.identity)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(snapshot.name)
        return _Certificate(context, replace(identity, pem_path=Path(self.config.tls_certfile)),
                            x509.load_pem_x509_certificate(cert).not_valid_after_utc, digest)

    async def watch(self, service, stop: asyncio.Event):
        failed = False
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=RELOAD_SECONDS)
                return
            except asyncio.TimeoutError:
                pass
            try:
                candidate = await asyncio.to_thread(self._read)
            except (OSError, ValueError, UnsupportedAlgorithm, x509.ExtensionNotFound,
                    x509.DuplicateExtension, x509.UnsupportedGeneralNameType):
                if not failed:
                    logger.warning("Mobile TLS update rejected; retaining the last validated certificate until expiry")
                failed = True
                continue
            changed = candidate is not self.current
            # Publication runs on the event loop, as do TLS callbacks. Existing
            # connections retain their context; device/session stores stay live.
            self.current = candidate
            service.identity = candidate.identity
            if changed or failed:
                logger.info("Mobile TLS certificate configuration loaded")
            failed = False

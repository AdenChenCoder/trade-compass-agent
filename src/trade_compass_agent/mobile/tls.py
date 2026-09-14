"""Browser TLS uses an explicitly configured origin; native identity stays stable."""
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import ipaddress
import ssl
from urllib.parse import urlsplit

from cryptography import x509
from cryptography.hazmat.primitives import hashes

from trade_compass_agent.config import MobileConfig
from trade_compass_agent.mobile.identity import ComputerIdentity


def browser_tls(config: MobileConfig, identity: ComputerIdentity):
    values = (config.public_origin, config.tls_certfile, config.tls_keyfile)
    if config.tls_relay and (not all(values) or not ipaddress.ip_address(config.host).is_loopback):
        raise ValueError("TLS relay requires loopback binding and a complete public TLS configuration")
    if not any(values):
        return identity, str(identity.pem_path)
    if not all(values):
        raise ValueError("PWA requires public_origin, tls_certfile and tls_keyfile")
    origin = urlsplit(config.public_origin)
    if (origin.scheme != "https" or not origin.hostname or origin.username or origin.password
            or origin.path or origin.query or origin.fragment
            or (not config.tls_relay and (origin.port or 443) != config.port)
            or (config.tls_relay and (origin.port or 443) != 443)):
        raise ValueError("PWA origin must be HTTPS on the mobile listener port")
    cert = x509.load_pem_x509_certificate(Path(config.tls_certfile).read_bytes())
    if not cert.not_valid_before_utc <= datetime.now(timezone.utc) < cert.not_valid_after_utc:
        raise ValueError("PWA certificate is not currently valid")
    names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    host = origin.hostname.lower()
    def matches(name):
        name = str(name).lower()
        return name == host or (name.startswith("*.") and host.count(".") == name.count(".")
                                and host.endswith(name[1:]))
    if not any(matches(name) for name in [*names.get_values_for_type(x509.DNSName),
                                         *names.get_values_for_type(x509.IPAddress)]):
        raise ValueError("PWA certificate does not match the configured origin")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(config.tls_certfile, config.tls_keyfile)
    # OS/browser trust is deliberately not inferred from successful server-side loading.
    return replace(identity, pem_path=Path(config.tls_certfile),
                   certificate_sha256=cert.fingerprint(hashes.SHA256()).hex()), config.tls_keyfile

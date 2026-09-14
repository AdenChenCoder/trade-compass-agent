from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from trade_compass_agent.concurrency import get_path_lock


@dataclass(frozen=True)
class ComputerIdentity:
    computer_id: str
    certificate_sha256: str
    pem_path: Path


def load_or_create_identity(directory: Path) -> ComputerIdentity:
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import NameOID
    except ImportError as exc:
        raise RuntimeError("Mobile HTTPS requires trade-compass-agent[mobile]") from exc

    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    path = directory / "identity.pem"
    with get_path_lock(path):
        if not path.exists():
            key = ec.generate_private_key(ec.SECP256R1())
            name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, uuid4().hex)])
            now = datetime.now(timezone.utc)
            cert = (
                x509.CertificateBuilder()
                .subject_name(name).issuer_name(name).public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(minutes=5))
                .not_valid_after(now + timedelta(days=3650))
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .sign(key, hashes.SHA256())
            )
            pem = cert.public_bytes(serialization.Encoding.PEM) + key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
            fd, temp_path = tempfile.mkstemp(dir=directory, prefix=".identity-")
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(pem)
                    handle.flush()
                    os.fsync(handle.fileno())
                try:
                    # Publish once, including when two service processes race to initialize.
                    os.link(temp_path, path)
                except FileExistsError:
                    pass
            finally:
                Path(temp_path).unlink(missing_ok=True)
        path.chmod(0o600)
        # A damaged/expired identity fails closed; never silently change the paired computer.
        cert = x509.load_pem_x509_certificate(path.read_bytes())
        if not cert.not_valid_before_utc <= datetime.now(timezone.utc) < cert.not_valid_after_utc:
            raise RuntimeError("Mobile TLS identity is not currently valid; re-pair after replacement")
    return ComputerIdentity(
        computer_id=cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value,
        certificate_sha256=cert.fingerprint(hashes.SHA256()).hex(),
        pem_path=path,
    )

"""Create an isolated LAN HTTPS test kit; never install trust or edit app config."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import ipaddress
import json
import os
from pathlib import Path
import uuid

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
import yaml


def create_test_kit(address: str, directory: Path, port: int = 19705) -> dict:
    ip = ipaddress.ip_address(address)
    if (not ip.is_private or ip.is_loopback or ip.is_unspecified or ip.is_multicast
            or ip.is_reserved or '%' in address):
        raise ValueError("请使用手机可访问的电脑局域网 IP，不要使用 localhost 或公网地址")
    if not 1 <= port <= 65535:
        raise ValueError("端口必须在 1–65535 之间")
    directory = directory.expanduser().absolute()
    # Never silently replace a certificate that a phone may already trust.
    directory.mkdir(parents=True, mode=0o700, exist_ok=False)
    now = datetime.now(timezone.utc)
    root_key = ec.generate_private_key(ec.SECP256R1())
    key = ec.generate_private_key(ec.SECP256R1())
    label = f"Trade Compass LAN Test {uuid.uuid4().hex[:8]}"
    root_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, label)])
    root = (x509.CertificateBuilder().subject_name(root_name).issuer_name(root_name)
            .public_key(root_key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=30))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=False, content_commitment=False,
                key_encipherment=False, data_encipherment=False, key_agreement=False,
                key_cert_sign=True, crl_sign=True, encipher_only=None, decipher_only=None), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(root_key.public_key()), critical=False)
            .sign(root_key, hashes.SHA256()))
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, str(ip))]))
            .issuer_name(root_name).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=30))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ip)]), critical=False)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False,
                key_encipherment=False, data_encipherment=False, key_agreement=False,
                key_cert_sign=False, crl_sign=False, encipher_only=None, decipher_only=None), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(root_key.public_key()), critical=False)
            .sign(root_key, hashes.SHA256()))
    host = f"[{ip}]" if ip.version == 6 else str(ip)
    origin = f"https://{host}:{port}"
    fragment = {"mobile": {"host": "::" if ip.version == 6 else "0.0.0.0", "port": port,
        "public_origin": origin, "tls_certfile": str(directory / "server.pem"),
        "tls_keyfile": str(directory / "server-key.pem")}}
    fingerprint = root.fingerprint(hashes.SHA256()).hex()
    instructions = f"""# 局域网 PWA 测试证书

手机地址：{origin}/mobile/
证书名称：{label}
根证书 SHA-256：{fingerprint}
有效期至：{cert.not_valid_after_utc.isoformat()}

1. 只把 public-ca.cer 传到自己的测试手机，安装并信任这个证书。
   iPhone：安装描述文件后，在“设置 → 通用 → 关于本机 → 证书信任设置”开启对应根证书的完全信任。
   Android：使用系统的 CA 证书安装入口；是否被所用浏览器信任仍需实际验证。
2. 将 mobile-fragment.yaml 中的字段合并到现有配置的 mobile 项，保留 enabled 和其他配置。
   该文件只是片段，不要用它替换完整配置，也不要把它作为 TRADE_COMPASS_CONFIG。
3. 重新运行项目，在电脑“设置 → 连接手机”开启连接并生成二维码。
4. 手机和电脑连接同一 Wi-Fi，确认上述地址没有证书警告，再扫码连接，并在手机输入电脑显示的配对码。
5. iPhone 可从 Safari 分享菜单添加到主屏幕；离线、系统推送和 Android 兼容性需要真机验证。

没有修改现有配置、系统信任、会话或运行服务。私钥 server-key.pem 只留在电脑，不能传给手机。
签发用的根私钥未保存。证书仅供 30 天开发测试；电脑 IP 改变或到期后应新建测试目录并重新信任。
结束测试后从手机移除名为“{label}”的证书，关闭手机连接，并恢复之前的 mobile 配置。
"""
    files = {
        "public-ca.cer": root.public_bytes(serialization.Encoding.DER),
        "server.pem": cert.public_bytes(serialization.Encoding.PEM) + root.public_bytes(serialization.Encoding.PEM),
        "server-key.pem": key.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8, serialization.NoEncryption()),
        "mobile-fragment.yaml": yaml.safe_dump(fragment, allow_unicode=True, sort_keys=False).encode(),
        "README.md": instructions.encode(),
    }
    for name, data in files.items():
        fd = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
    return {"directory": str(directory), "mobile_url": f"{origin}/mobile/",
            "ca_certificate": str(directory / "public-ca.cer"), "ca_sha256": fingerprint,
            "instructions": str(directory / "README.md"), "expires_at": cert.not_valid_after_utc.isoformat()}


def main() -> None:
    parser = argparse.ArgumentParser(description="生成局域网 PWA 开发证书，不修改系统信任或运行配置")
    parser.add_argument("--address", required=True, help="电脑当前的局域网 IP")
    parser.add_argument("--output", required=True, type=Path, help="全新的测试目录；不会覆盖已有目录")
    parser.add_argument("--port", type=int, default=19705)
    args = parser.parse_args()
    try:
        result = create_test_kit(args.address, args.output, args.port)
    except (ValueError, OSError) as exc:
        parser.exit(1, f"未完成：{exc}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

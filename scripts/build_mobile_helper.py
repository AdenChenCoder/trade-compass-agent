"""Build the pinned, offline-at-install-time mobile connection components."""
from __future__ import annotations

import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "scripts/mobile-funnel-probe"
BUNDLE = ROOT / "src/trade_compass_agent/mobile_bin"
GO_VERSION = "go1.27.1"
TARGETS = ("darwin-arm64", "darwin-amd64", "linux-arm64", "linux-amd64")


def source_digest():
    digest = hashlib.sha256(Path(__file__).read_bytes())
    for path in sorted([*SOURCE.glob("*.go"), SOURCE / "go.mod", SOURCE / "go.sum"]):
        digest.update(path.name.encode() + b"\0" + path.read_bytes())
    return digest.hexdigest()


def bundle_current():
    try:
        manifest = json.loads((BUNDLE / "manifest.json").read_text())
        return (manifest["source_sha256"] == source_digest()
                and set(manifest["platforms"]) == set(TARGETS)
                and all(hashlib.sha256((BUNDLE / (target + ".gz")).read_bytes()).hexdigest()
                        == manifest["platforms"][target]["archive_sha256"] for target in TARGETS)
                and hashlib.sha256((BUNDLE / "LICENSES.txt").read_bytes()).hexdigest() == manifest["licenses_sha256"])
    except (OSError, ValueError, KeyError):
        return False


def build():
    if bundle_current():
        return
    go = os.environ.get("TRADE_COMPASS_GO") or shutil.which("go")
    if not go:
        raise RuntimeError("Building from source requires Go; installed wheels already contain the mobile helper")
    env = {**os.environ, "GOTOOLCHAIN": GO_VERSION, "CGO_ENABLED": "0"}

    def run(args, target_env=env):
        return subprocess.run([go, *args], cwd=SOURCE, env=target_env, check=True,
                              text=True, capture_output=True).stdout.strip()

    if not run(["version"]).startswith("go version " + GO_VERSION + " "):
        raise RuntimeError("Unexpected Go toolchain")
    module = json.loads(run(["list", "-mod=readonly", "-m", "-json", "tailscale.com"]))
    if module.get("Replace") or not re.fullmatch(r"v\d+\.\d+\.\d+", module["Version"]):
        raise RuntimeError("Mobile component requires a pinned, unmodified Tailscale release")
    upstream_version = module["Version"][1:]
    digest = source_digest()
    tailscale_version = f"{upstream_version}-compass.{digest[:12]}"
    manifest = {"protocol": 1, "toolchain": GO_VERSION, "source_sha256": digest,
                "tailscale_version": tailscale_version, "platforms": {}}
    # These are Tailscale's supported version stamps, not this repository's Git identity.
    ldflags = (f"-s -w -X tailscale.com/version.longStamp={tailscale_version}"
               f" -X tailscale.com/version.shortStamp={upstream_version}")
    modules = set()
    BUNDLE.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="compass-helper-") as temporary:
        for target in TARGETS:
            goos, goarch = target.split("-")
            target_env = {**env, "GOOS": goos, "GOARCH": goarch}
            executable = Path(temporary) / target
            print("Building mobile connection component: " + target, flush=True)
            run(["build", "-trimpath", "-buildvcs=false", "-ldflags=" + ldflags, "-o", str(executable), "."], target_env)
            binary = executable.read_bytes()
            archive = gzip.compress(binary, compresslevel=9, mtime=0)
            (BUNDLE / (target + ".gz")).write_bytes(archive)
            manifest["platforms"][target] = {"sha256": hashlib.sha256(binary).hexdigest(),
                "size": len(binary), "archive_sha256": hashlib.sha256(archive).hexdigest()}
            dependencies = run(["list", "-deps", "-f", "{{with .Module}}{{.Path}}\t{{.Version}}\t{{.Dir}}{{end}}", "."], target_env)
            modules.update(tuple(line.split("\t")) for line in dependencies.splitlines() if line.strip())
        notices = ["Trade Compass mobile connection component\n\nGo runtime:\n"
                   + (Path(run(["env", "GOROOT"])) / "LICENSE").read_text()]
        for module, version, directory in sorted(modules):
            if Path(directory).resolve() == SOURCE.resolve():
                continue  # The main program is covered by the project's MIT license.
            licenses = sorted(p for p in Path(directory).iterdir() if p.is_file()
                              and p.name.lower().startswith(("license", "copying", "notice")))
            if not licenses:
                raise RuntimeError(f"Missing bundled dependency license: {module}@{version}")
            notices.append(f"\n\n{module}@{version}\n" + "\n".join(p.read_text() for p in licenses))
        notice_bytes = "".join(notices).encode()
        (BUNDLE / "LICENSES.txt").write_bytes(notice_bytes)
        manifest["licenses_sha256"] = hashlib.sha256(notice_bytes).hexdigest()
        (BUNDLE / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")


if __name__ == "__main__":
    build()

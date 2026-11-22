"""Replay SDK recovery defects without providers, models or physical link changes."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
GO_ROOT = ROOT / "scripts/mobile-funnel-probe"
FIXTURES = GO_ROOT / "testdata/recovery"
SCENES = {
    "net/netmon": "TestNetworkComesUpAtSubscriptionBoundary",
    "wgengine": "TestDiscoUpdatePreservesExistingInboundSession",
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--go", default=os.environ.get("TRADE_COMPASS_GO") or shutil.which("go"))
    args = parser.parse_args()
    if not args.go:
        parser.error("a native Go toolchain is required")
    args.output = args.output.resolve()
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    env = {key: os.environ[key] for key in ("HOME", "USER", "LOGNAME", "PATH", "LANG", "TMPDIR")
           if key in os.environ}
    env.update(GOTOOLCHAIN="local", GOMAXPROCS="2", GOFLAGS="-p=2",
               GOPROXY="https://proxy.golang.org,direct", GOSUMDB="sum.golang.org",
               GOPRIVATE="", GONOPROXY="", GONOSUMDB="")

    def go(arguments, cwd):
        return subprocess.run([args.go, *arguments], cwd=cwd, env=env, capture_output=True,
                              text=True, timeout=240)

    module = go(["list", "-m", "-json", "tailscale.com"], GO_ROOT)
    module.check_returncode()
    target = json.loads(module.stdout)
    if target.get("Replace") or target["Version"] != "v1.104.7":
        raise RuntimeError("acceptance requires the pinned unmodified v1.104.7 candidate")
    platform = go(["env", "GOOS"], GO_ROOT)
    platform.check_returncode()
    platform = platform.stdout.strip()
    if platform not in {"darwin", "linux"}:
        raise RuntimeError("the OS notification fixture supports native Darwin or Linux")

    results = []
    for version, operation in (("v1.102.4", "ResetDevicePeer"),
                               (target["Version"], "MarkDevicePeerForHandshake")):
        directory = args.output / version
        directory.mkdir()
        (directory / "go.mod").write_text(
            "module compass.local/recovery-acceptance\n\ngo 1.27.4\n")
        download = go(["mod", "download", "-json", "tailscale.com@" + version], directory)
        download.check_returncode()
        provenance = json.loads(download.stdout)
        source = Path(provenance["Dir"])
        sdk = directory / "sdk-source"
        shutil.copytree(source, sdk)
        # Source copies and compiler overlays keep the module cache immutable.
        (directory / "go.mod").write_text(
            "module compass.local/recovery-acceptance\n\ngo 1.27.4\n"
            f"require tailscale.com {version}\nreplace tailscale.com => {sdk}\n")
        (directory / "go.sum").write_bytes((source / "go.sum").read_bytes())
        netmon = (source / "net/netmon/netmon.go").read_text()
        getter = "return getState(tsIfProps.tsIfName())"
        if netmon.count(getter) != 1:
            raise RuntimeError("unsupported SDK interface-state fixture")
        (directory / "netmon.go").write_text(netmon.replace(
            getter, "return &State{HaveV4: reproLinkUp}, nil", 1))
        os_file = f"net/netmon/netmon_{platform}.go"
        os_source = (source / os_file).read_text()
        if os_source.count("func newOSMon(") != 1:
            raise RuntimeError("unsupported SDK subscription fixture")
        (directory / "os.go").write_text(os_source.replace(
            "func newOSMon(", "func originalOSMonUnused(", 1))
        (directory / "engine_test.go").write_text(
            (FIXTURES / "engine_case_test.go").read_text().replace("RECOVERY_OPERATION", operation))
        overlay = {
            str(sdk / "net/netmon/netmon.go"): str(directory / "netmon.go"),
            str(sdk / os_file): str(directory / "os.go"),
            str(sdk / "net/netmon/root_repro_hook.go"): str(FIXTURES / "netmon_hook.go"),
            str(sdk / "net/netmon/root_repro_test.go"): str(FIXTURES / "netmon_case_test.go"),
            str(sdk / "wgengine/root_session_repro_test.go"): str(directory / "engine_test.go"),
        }
        overlay_file = directory / "overlay.json"
        overlay_file.write_text(json.dumps({"Replace": overlay}))
        for package, scene in SCENES.items():
            result = go(["test", "-mod=mod", "-overlay", str(overlay_file), "-json",
                         "-run", "^" + scene + "$", "-count=1", "-timeout=45s",
                         "tailscale.com/" + package], directory)
            name = package.replace("/", "_")
            (directory / (name + ".jsonl")).write_text(result.stdout)
            (directory / (name + ".stderr")).write_text(result.stderr)
            events = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
            outcomes = [event["Action"] for event in events
                        if event.get("Test") == scene and event["Action"] in {"pass", "fail", "skip"}]
            evidence = []
            for event in events:
                output = event.get("Output", "")
                start = output.find("{")
                if start < 0:
                    continue
                try:
                    value = json.loads(output[start:])
                except json.JSONDecodeError:
                    continue
                if value.get("recovery_scene") in {"network_availability", "session_continuity"}:
                    evidence.append(value)
            expected = "fail" if version == "v1.102.4" else "pass"
            expected_value = version != "v1.102.4"
            scenario_valid = False
            if len(evidence) == 1:
                value = evidence[0]
                if package == "net/netmon":
                    scenario_valid = (value.get("actual_link_up") is True
                                      and value.get("cached_link_up") is expected_value)
                else:
                    scenario_valid = (value.get("baseline_bidirectional") is True
                                      and value.get("incoming_packet_delivered") is expected_value)
            valid = (scenario_valid and outcomes == [expected]
                     and result.returncode == (1 if expected == "fail" else 0))
            results.append({"version": version, "source_sum": provenance["Sum"], "scene": scene,
                            "observed": outcomes, "returncode": result.returncode,
                            "evidence": evidence, "accepted": valid})
            (args.output / "results.json").write_text(json.dumps(results, indent=2))
            if not valid:
                raise RuntimeError("recovery scene incomplete or unexpected; evidence retained")
    print("Both actual-SDK recovery defects reproduced before and passed after; no provider/model calls.")


if __name__ == "__main__":
    main()

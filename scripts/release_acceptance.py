"""Run the release contract against an exact, already installed wheel.

No source imports, production state, model service or real tools are needed.
Output directories are exclusive: failed attempts cannot be overwritten.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
import zipfile


SUITES = (
    "test_report_transport.py",
    "test_report_publication.py",
    "test_report_reading.py",
    "test_report_scheduler_contract.py",
    "test_report_batch_contract.py",
    "test_report_incident_annotations.py",
    "test_native_agents.py",
    "test_native_budgets.py",
)


def check_junit(path: Path) -> dict[str, int]:
    """An empty, skipped or failing suite is not release evidence."""
    suites = list(ET.parse(path).getroot().iter("testsuite"))
    counts = {key: sum(int(s.get(key, "0")) for s in suites)
              for key in ("tests", "failures", "errors", "skipped")}
    if counts["tests"] == 0 or any(counts[k] for k in ("failures", "errors", "skipped")):
        raise RuntimeError(f"release acceptance did not pass: {counts}")
    return counts


def verify_installed_wheel(wheel: Path) -> dict[str, object]:
    import trade_compass_agent
    from trade_compass_agent.config import is_source_checkout

    package = Path(trade_compass_agent.__file__).resolve().parent
    if is_source_checkout():
        raise RuntimeError("acceptance must import an installed wheel outside the checkout")
    with zipfile.ZipFile(wheel) as archive:
        names = [n for n in archive.namelist()
                 if n.startswith("trade_compass_agent/") and not n.endswith("/")]
        if not names:
            raise RuntimeError("wheel has no application package")
        for name in names:
            target = package / Path(name).relative_to("trade_compass_agent")
            if not target.is_file() or target.read_bytes() != archive.read(name):
                raise RuntimeError(f"installed file differs from candidate wheel: {name}")
    return {"version": trade_compass_agent.__version__, "package": str(package),
            "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
            "verified_files": len(names)}


def run(wheel: Path, output: Path) -> dict[str, object]:
    source = Path(__file__).resolve().parents[1]
    output = output.resolve()
    if output.is_relative_to(source):
        raise RuntimeError("acceptance output must be outside the source checkout")
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    result: dict[str, object] = {"passed": False, "mode": "deterministic",
                               "model": "explicit doubles", "business_tools": "frozen",
                               "python": sys.version,
                               "suite_sha256": {name: hashlib.sha256((source/"tests"/name).read_bytes()).hexdigest()
                                                for name in (*SUITES, "conftest.py", "report_contract_helpers.py")}}
    try:
        os.chdir(output)
        result.update(verify_installed_wheel(wheel))
        copied = output / "tests"
        copied.mkdir()
        for name in (*SUITES, "conftest.py", "report_contract_helpers.py"):
            shutil.copyfile(source / "tests" / name, copied / name)
        # A fresh config and every writable root are explicit; HOME is untouched.
        (output / "config.yaml").write_text("llm:\n  provider: disabled\ndata_provider: sample\n")
        (output / ".env").touch()
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("TRADE_COMPASS_") and k not in {"PYTHONPATH", "PYTEST_ADDOPTS"}}
        env.update(TRADE_COMPASS_HOME=str(output / "home"),
                   TRADE_COMPASS_CONFIG=str(output / "config.yaml"),
                   TRADE_COMPASS_ENV_FILE=str(output / ".env"),
                   TRADE_COMPASS_DATA_DIR=str(output / "data"),
                   TRADE_COMPASS_MEMORY_DIR=str(output / "memory"),
                   TRADE_COMPASS_NO_SCHEDULER="true", MPLBACKEND="Agg",
                   PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
        command = [sys.executable, "-I", "-m", "pytest", "-q", "--strict-markers",
                   "--junitxml=" + str(output / "junit.xml"), *[str(copied / n) for n in SUITES]]
        with (output / "pytest.log").open("w") as log:
            completed = subprocess.run(command, cwd=output, env=env, stdout=log,
                                       stderr=subprocess.STDOUT, timeout=300)
        result["exit_code"] = completed.returncode
        result["counts"] = check_junit(output / "junit.xml")
        if completed.returncode:
            raise RuntimeError(f"pytest exited {completed.returncode}")
        result["passed"] = True
        return result
    except Exception as exc:
        result["error"] = str(exc)
        raise
    finally:
        evidence = output / "acceptance.json"
        evidence.write_text(json.dumps(result, ensure_ascii=False, indent=2))
        evidence.chmod(0o600)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.wheel.resolve(), args.output), ensure_ascii=False))


if __name__ == "__main__":
    main()

"""The unit-test boundary must fail even when application fallback hides a call."""
from pathlib import Path
import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET


def test_swallowed_live_model_calls_fail_while_explicit_doubles_pass(tmp_path):
    source = Path(__file__).resolve().parent
    shutil.copyfile(source / "conftest.py", tmp_path / "conftest.py")
    (tmp_path / "test_probe.py").write_text('''
import socket
import pytest

@pytest.mark.parametrize("port", [1234, 11434])
def test_swallowed_model_connection(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.1):
            pass
    except Exception:
        pass  # Simulate an application returning a successful fallback.

def test_explicit_double(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", lambda *args: None)
    with socket.socket() as client:
        client.connect(("127.0.0.1", 1234))
''')
    env = dict(os.environ, PYTHONPATH=str(source.parent / "src"), PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
    for key in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS"):
        env.pop(key, None)
    result = subprocess.run([sys.executable, "-m", "pytest", "-q", "--junitxml=result.xml", "test_probe.py"],
                            cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 1, result.stdout + result.stderr
    cases = list(ET.parse(tmp_path / "result.xml").getroot().iter("testcase"))
    rejected = [case for case in cases if case.find("error") is not None]
    assert len(rejected) == 2 and all("swallowed_model_connection" in case.attrib["name"] for case in rejected)
    assert all("inject a model double" in case.find("error").attrib["message"] for case in rejected)
    explicit = next(case for case in cases if case.attrib["name"] == "test_explicit_double")
    assert explicit.find("error") is None and explicit.find("failure") is None

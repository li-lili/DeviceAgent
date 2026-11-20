"""Configuration-only entry point: malformed settings fail before connecting."""
from pathlib import Path
from contextlib import contextmanager
import socketserver
import subprocess
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def local_listener():
    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            pass
    with socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler) as server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            yield server.server_address[1]
        finally:
            server.shutdown()


def assert_waiting(directory):
    output = directory / "waiting.log"
    with output.open("w") as sink:
        process = subprocess.Popen([str(ROOT / "build/device-agent")], cwd=directory,
                                   stdout=sink, stderr=subprocess.STDOUT)
    try:
        deadline = time.monotonic() + 3
        while process.poll() is None and "IDENTITY waiting" not in output.read_text() and time.monotonic() < deadline:
            time.sleep(.02)
        assert process.poll() is None and "IDENTITY waiting" in output.read_text()
        process.terminate()
        process.wait(timeout=3)
        assert process.returncode == 0 and "ENROLL requesting" not in output.read_text()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def main():
    cases = [
        (None, "cannot open config"),
        ("environment=production", "environment must be local or cloud"),
        ("environment=cloud", "cannot open config"),
        ("environment=local" + chr(10) + "port=1883", "cannot be mixed"),
        ("allowed-root=logs" + chr(10) + "allow-insecure-loopback-enroll=yes", "expected true or false"),
        ("config-file=missing.conf", "cannot open config"),
        ("config-file=device-agent.conf", "nesting too deep"),
        ("config-file=profile.conf" + chr(10) + "port=1883", "cannot be mixed"),
        ("config-file=", "must not be empty"),
        ("device=bad sn!", "invalid device"),
        ("allowed-root=logs\nunknown-setting=true", "unknown config key"),
        ("allowed-root=logs\nport=0", "port must be"),
        ("allowed-root=logs\nport=18883junk", "port must be"),
        ("allowed-root=logs\nlocal-port=65536", "port must be"),
        ("allowed-root=logs\nallow-insecure-loopback-upload=yes", "expected true or false"),
        ("allowed-root=logs\nstate-dir=", "must not be empty"),
    ]
    for text, error in cases:
        with tempfile.TemporaryDirectory(prefix="agent-config-") as temp:
            directory = Path(temp)
            if text is not None:
                baseline = "host=127.0.0.1\nport=9\ndevice=config-test\nstate-dir=state\nwork-dir=work\n"
                if "environment=" in text or "config-file=" in text:
                    baseline = ""
                (directory / "device-agent.conf").write_text(baseline + text.replace(chr(92) + "n", chr(10)) + chr(10))
            result = subprocess.run([str(ROOT / "build/device-agent")],
                                    cwd=directory, capture_output=True, text=True, timeout=5)
            assert result.returncode == 1 and error in (result.stdout + result.stderr), result

    # SN 不写明文：device-file 指向只含 SN 的文件；读取成功后 Token 缓存路径使用该 SN。
    with local_listener() as port, tempfile.TemporaryDirectory(prefix="agent-device-file-") as temp:
        directory = Path(temp)
        baseline = f"host=127.0.0.1\nport={port}\nstate-dir=state\nwork-dir=work\n"
        sn_file = directory / "sn.txt"
        config = directory / "device-agent.conf"
        sn_file.write_text("file-only-device\n")
        config.write_text(baseline + f"device-file=sn.txt\nenroll-url=https://127.0.0.1:{port}/v1/enroll\n")
        token = directory / "state/tokens/file-only-device.token"
        token.parent.mkdir(parents=True)
        token.touch()
        result = subprocess.run([str(ROOT / "build/device-agent")], cwd=directory,
                                capture_output=True, text=True, timeout=5)
        out = result.stdout + result.stderr
        assert result.returncode == 1 and "invalid token file" in out and "file-only-device.token" in out, out

        config.write_text(baseline + "device=other-device\ndevice-file=sn.txt\n")
        result = subprocess.run([str(ROOT / "build/device-agent")], cwd=directory,
                                capture_output=True, text=True, timeout=5)
        assert result.returncode == 1 and "device conflicts with device-file sn" in (result.stdout + result.stderr), result

        sn_file.write_text("bad sn!\n")
        config.write_text(baseline + "device-file=sn.txt\n")
        assert_waiting(directory)
    print("PASS device-file supplies sn without plaintext device settings")

    # Minimal cloud profile applies built-in paths. A corrupt local cache stops
    # before any real registration or MQTT traffic, so this check is offline.
    with local_listener() as port, tempfile.TemporaryDirectory(prefix="agent-minimal-profile-") as temp:
        directory = Path(temp)
        (directory / "device-agent.conf").write_text("environment=cloud" + chr(10))
        # enrollment-key 已废弃（仅告警忽略）；注册配置由 enroll-url/private-key 决定，
        # 该离线用例在损坏的 Token 缓存处失败，不会发起网络请求，也不会回显注册码。
        (directory / "device-agent.cloud.conf").write_text(
            f"device=offline-profile-test\nhost=127.0.0.1\nport={port}\nmodel=test-model\nstate-dir=run/cloud-agent\nwork-dir=run/cloud-work\nsources-config=log-sources.json\nenroll-url=https://127.0.0.1:{port}/v1/enroll\nenrollment-key=enr_synthetic\n")
        cache = directory / "run/cloud-agent/tokens/offline-profile-test.token"
        cache.parent.mkdir(parents=True)
        cache.touch()
        result = subprocess.run([str(ROOT / "build/device-agent")], cwd=directory,
                                capture_output=True, text=True, timeout=5)
        assert result.returncode == 1 and "invalid token file" in (result.stdout + result.stderr), result
        assert str(cache) in (result.stdout + result.stderr) and "environment=cloud" in (result.stdout + result.stderr)
        assert "enr_synthetic" not in (result.stdout + result.stderr)

        (directory / "device-agent.cloud.conf").write_text("device=bad sn!\nenrollment-key=enr_synthetic" + chr(10))
        result = subprocess.run([str(ROOT / "build/device-agent")], cwd=directory,
                                capture_output=True, text=True, timeout=5)
        assert result.returncode == 1 and "invalid device" in (result.stdout + result.stderr)
    print("PASS explicit cloud profile loads settings without exposing credentials or making network calls")
    print("PASS config-only entry rejects missing/invalid settings and reports startup errors")


if __name__ == "__main__":
    main()

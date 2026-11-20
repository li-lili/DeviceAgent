"""Loopback-only checks for provisioning identity while the agent is running."""
import http.server
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
BINARY = Path(os.environ.get("DEVICE_AGENT_TEST_BINARY", ROOT / "build/device-agent")).resolve()
MOCK = Path(os.environ.get("DEVICE_AGENT_FACTORY_MOCK", ROOT / "build/libfactory_service_mock.so")).resolve()


def wait_until(check, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(.05)
    raise AssertionError("timed out waiting for identity state")


def main():
    requests = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            data = b'{"error":{"code":"rate_limited"}}'
            self.send_response(429)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory(prefix="identity-wait-") as tmp:
            root = Path(tmp)
            generated = root / "generated.pem"
            subprocess.run(["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt",
                            "rsa_keygen_bits:2048", "-out", str(generated)], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            key_bytes = generated.read_bytes()
            for case in ("file-sn", "file-key", "vendor-sn", "vendor-key", "partial-key", "stop-sn", "stop-key"):
                directory = root / case
                directory.mkdir()
                sn_file, key_file = directory / "sn.txt", directory / "private.pem"
                missing_sn = case.endswith("sn")
                if not missing_sn:
                    sn_file.write_text("provisioned-test-device")
                if missing_sn:
                    key_file.write_bytes(key_bytes)
                elif case == "partial-key":
                    key_file.write_text("-----BEGIN PRIVATE KEY-----\npartial")
                vendor = case.startswith("vendor")
                env = dict(os.environ, FACTORY_MOCK_SN_FILE=str(sn_file), FACTORY_MOCK_KEY_FILE=str(key_file))
                config = (
                    f"host=127.0.0.1\nport={server.server_port}\n"
                    f"enroll-url=http://127.0.0.1:{server.server_port}/v1/enroll\n"
                    "allow-insecure-loopback-enroll=true\nstate-dir=state\nwork-dir=work\nlog-file=agent.log\n"
                )
                if vendor:
                    config += f"identity-source=vendor-storage\nfactory-lib={MOCK}\n"
                else:
                    config += f"device-file={sn_file}\nprivate-key={key_file}\n"
                (directory / "device-agent.conf").write_text(config)
                log = directory / "agent.log"
                before = len(requests)
                with (directory / "stdout.log").open("w") as output:
                    process = subprocess.Popen([str(BINARY)], cwd=directory, env=env,
                                               stdout=output, stderr=subprocess.STDOUT)
                try:
                    wait_until(lambda: log.exists() and "IDENTITY waiting" in log.read_text())
                    assert process.poll() is None, "agent exited while identity was missing"
                    assert len(requests) == before, "sent cloud request before identity was ready"
                    assert not list((directory / "state").rglob("*.tmp.*")), "temporary Token created while waiting"
                    if case.startswith("stop"):
                        process.send_signal(signal.SIGTERM)
                        process.wait(timeout=3)
                        assert process.returncode == 0
                        assert "STOP device-agent while waiting for identity" in log.read_text()
                        assert len(requests) == before
                    else:
                        if missing_sn:
                            sn_file.write_text("provisioned-test-device")
                        else:
                            key_file.write_bytes(key_bytes)
                        process.wait(timeout=15)
                        assert process.returncode == 1, "expected one 429 response to end enrollment"
                        assert len(requests) == before + 1, "registration skipped or retried"
                        assert requests[-1]["sn"] == "provisioned-test-device"
                        text = log.read_text()
                        assert text.index("IDENTITY waiting") < text.index("ENROLL requesting")
                        assert '"http_status":429' in text
                        assert "BEGIN PRIVATE KEY" not in text
                    print("PASS:", case)
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait()
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()

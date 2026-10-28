"""Loopback-only response/log/cache tests with a disposable synthetic identity."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
BINARY = Path(os.environ.get("DEVICE_AGENT_TEST_BINARY", ROOT / "build-native/device-agent"))


def require(value, label):
    if not value:
        raise AssertionError(label)  # Never print process output or synthetic credentials.


def main():
    with tempfile.TemporaryDirectory(prefix="agent-response-offline-") as name:
        directory = Path(name)
        key = directory / "synthetic-key.pem"
        subprocess.run(["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048",
                        "-out", str(key)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        key.chmod(0o600)
        token = "FAKE_LOOPBACK_ACCESS_VALUE"
        mode, requests = [None], []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                require(self.path == "/v1/enroll", "unexpected local request path")
                requests.append(request)
                code, body = mode[0]
                if isinstance(body, dict):
                    body = dict(body)
                    # Echo only this disposable request signature to prove log redaction.
                    body["signature"] = request["signature"]
                    body["extra"] = "FAKE_EXTRA_RESPONSE_FIELD"
                    if isinstance(body.get("error"), dict):
                        body["error"] = dict(body["error"])
                        body["error"]["message"] = "registration conflict; echo " + request["signature"]
                    encoded = json.dumps(body).encode()
                else:
                    encoded = body
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        # A bound, non-listening socket prevents unrelated MQTT access in success tests.
        with socket.socket() as mqtt_closed:
            mqtt_closed.bind(("127.0.0.1", 0))
            port = mqtt_closed.getsockname()[1]
            cases = [(200, {"token": token, "message": "accepted"}, True, None),
                     (201, {"token": token, "message": "created"}, True, None),
                     (400, {"error": {"code": "bad_request"}}, False, "bad_request"),
                     (401, {"error": "unauthorized", "message": "identity denied"}, False, "unauthorized"),
                     (409, {"error": {"code": "already_enrolled"}}, False, "already_enrolled"),
                     (409, {"error": {"code": "device_identity_already_exists"}}, False, "device_identity_already_exists"),
                     (429, {"error": {"code": "rate_limited"}}, False, "rate_limited"),
                     (502, b"<html>FAKE_GATEWAY_BODY</html>", False, None),
                     (200, b"not json", False, None), (201, {}, False, None),
                     (200, {"token": token, "error": {"code": "unauthorized"}}, False, "unauthorized"),
                     (202, {"token": token}, False, None)]
            try:
                for index, (code, body, success, expected_code) in enumerate(cases):
                    mode[0] = code, body
                    state = directory / f"state-{index}"
                    log = directory / f"agent-{index}.log"
                    cache = state / "tokens/offline-response-device.token"
                    (directory / "sources.json").write_text('{"log_sources": []}')
                    (directory / "device-agent.conf").write_text(
                        f"device=offline-response-device\nhost=127.0.0.1\nport={port}\n"
                        f"private-key={key}\nenroll-url=http://127.0.0.1:{server.server_port}/v1/enroll\n"
                        f"allow-insecure-loopback-enroll=true\nstate-dir={state}\nwork-dir={directory}/work-{index}\n"
                        f"log-file={log}\nsources-config=sources.json\n")
                    before = len(requests)
                    process = subprocess.Popen([str(BINARY)], cwd=directory, stdout=subprocess.PIPE,
                                               stderr=subprocess.STDOUT, text=True)
                    try:
                        deadline = time.monotonic() + 5
                        while process.poll() is None and not cache.exists() and time.monotonic() < deadline:
                            time.sleep(.02)
                        if success:
                            require(cache.exists(), "success did not save test Token")
                            require(stat.S_IMODE(cache.stat().st_mode) == 0o600, "incorrect cache permissions")
                            require(cache.read_text() == token, "cache differs from synthetic Token")
                        else:
                            process.wait(timeout=5)
                            require(process.returncode == 1, "rejected response did not fail")
                            require(not cache.exists(), "rejected response saved Token")
                    finally:
                        if process.poll() is None:
                            process.terminate()
                        output = process.communicate(timeout=5)[0]
                    require(len(requests) == before + 1, "unexpected retry or skipped request")
                    require(set(requests[-1]) == {"sn", "signature"}, "request protocol changed")
                    require(not list(state.glob("tokens/*.tmp.*")), "temporary cache left after failure")
                    text = output + (log.read_text() if log.exists() else "")
                    for secret in (token, requests[-1]["signature"], "FAKE_EXTRA_RESPONSE_FIELD", "FAKE_GATEWAY_BODY"):
                        require(secret not in text, "credential or raw response leaked")
                    lines = [line.split("ENROLL response ", 1)[1] for line in output.splitlines()
                             if "ENROLL response " in line]
                    require(len(lines) == 1, "expected one diagnostic record per request")
                    diagnostic = json.loads(lines[0])
                    require(diagnostic["http_status"] == code, "wrong HTTP status in actual log")
                    require(diagnostic.get("error_code") == expected_code, "wrong original error code in actual log")
                    if expected_code in ("already_enrolled", "device_identity_already_exists"):
                        require(diagnostic["error_message"].startswith("registration conflict"), "message context lost")
                    print(f"PASS loopback case {index+1}: HTTP {code}; safe log and cache outcome")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
        print("PASS 12 isolated local response cases; no real cloud, ADB or existing credentials accessed")


if __name__ == "__main__":
    main()

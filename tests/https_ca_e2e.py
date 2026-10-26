"""Custom HTTPS CA must be trusted before enrollment; never contacts the real cloud."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import json
import os
import ssl
import subprocess
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
BINARY = Path(os.environ.get("DEVICE_AGENT_TEST_BINARY", ROOT / "build/device-agent"))


def main():
    with tempfile.TemporaryDirectory(prefix="agent-https-ca-") as temp:
        directory = Path(temp)
        certificate, key = directory / "ca.pem", directory / "key.pem"
        subprocess.run([
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
            "-subj", "/CN=localhost", "-addext", "subjectAltName=IP:127.0.0.1",
            "-keyout", str(key), "-out", str(certificate),
        ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        requests = []
        token = "synthetic-https-device-token"

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                reply = json.dumps({"token": token}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(reply)))
                self.end_headers()
                self.wfile.write(reply)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certificate, key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            settings = (
                "host=127.0.0.1\nport=9\ndevice=https-ca-test\nallowed-root=logs\n"
                "sources-config=sources.json\nprivate-key=key.pem\n"
                "model=test-model\nstate-dir=run/agent\nwork-dir=run/work\n"
                f"enroll-url=https://127.0.0.1:{server.server_port}/v1/enroll\n"
            )
            (directory / "sources.json").write_text('{"log_sources": []}')
            config = directory / "device-agent.conf"
            config.write_text(settings)
            rejected = subprocess.run([str(BINARY)], cwd=directory, capture_output=True, text=True, timeout=20)
            assert rejected.returncode == 1 and "enrollment request failed" in (rejected.stdout + rejected.stderr), rejected
            assert not requests, "An untrusted TLS server received enrollment credentials"
            cache = directory / "run/agent/tokens/https-ca-test.token"
            assert not cache.exists()

            config.write_text(settings + f"https-ca={certificate}\n")
            process = subprocess.Popen([str(BINARY)], cwd=directory, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, text=True)
            try:
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline and not cache.exists() and process.poll() is None:
                    time.sleep(0.05)
                assert cache.exists() and cache.read_text() == token
            finally:
                if process.poll() is None:
                    process.terminate()
                output = process.communicate(timeout=10)[0]
            assert "ENROLL succeeded" in output, output
            assert len(requests) == 1 and requests[0]["sn"] == "https-ca-test"
            assert set(requests[0].keys()) == {"sn", "signature"}
            assert token not in output and requests[0]["signature"] not in output

            config.write_text(settings + "https-ca=missing-ca.pem\n")
            missing = subprocess.run([str(BINARY)], cwd=directory, capture_output=True, text=True, timeout=5)
            assert missing.returncode == 1 and "HTTPS CA bundle is missing" in (missing.stdout + missing.stderr)
            assert len(requests) == 1
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
    print("PASS HTTPS enrollment rejects untrusted servers, accepts configured CA, and keeps credentials private")


if __name__ == "__main__":
    main()

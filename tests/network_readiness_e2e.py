"""Local-only startup gate checks; no real device identity or cloud requests."""
import http.server
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time

BINARY = Path(os.environ.get("DEVICE_AGENT_TEST_BINARY", "build/device-agent")).resolve()


def reserve_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    return sock


def main():
    requests = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            requests.append(self.path)
            body = b'{"error":{"code":"rate_limited"}}'
            self.send_response(429)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    with tempfile.TemporaryDirectory(prefix="agent-network-") as tmp:
        root = Path(tmp)
        key = root / "key.pem"
        subprocess.run(["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt",
                        "rsa_keygen_bits:2048", "-out", str(key)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for blocked in ("mqtt", "wss", "enroll", "stop"):
            requests.clear()
            mqtt = reserve_port()
            enroll = reserve_port()
            mqtt_port, enroll_port = mqtt.getsockname()[1], enroll.getsockname()[1]
            if blocked == "enroll":
                mqtt.listen()
            server = None
            if blocked != "enroll":
                enroll.close()
                server = http.server.ThreadingHTTPServer(("127.0.0.1", enroll_port), Handler)
                threading.Thread(target=server.serve_forever, daemon=True).start()
            (root / "device-agent.conf").write_text(
                f"device=network-test-device\nhost=127.0.0.1\nport={mqtt_port}\n"
                + (f"mqtt-url=wss://127.0.0.1:{mqtt_port}/mqtt\n" if blocked == "wss" else "")
                +
                f"enroll-url=http://127.0.0.1:{enroll_port}/v1/enroll\n"
                f"private-key={key}\nallow-insecure-loopback-enroll=true\n"
                f"state-dir={root / blocked}\nwork-dir={root / 'work'}\n")
            process = subprocess.Popen([str(BINARY)], cwd=root, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, text=True)
            try:
                time.sleep(1)
                assert process.poll() is None, "agent exited instead of waiting"
                assert not requests, "enrollment sent before both endpoints were ready"
                if blocked == "stop":
                    process.terminate()
                    output = process.communicate(timeout=7)[0]
                    assert process.returncode == 0, output
                    assert "STOP device-agent while waiting for network" in output, output
                    assert not requests
                else:
                    if blocked in ("mqtt", "wss"):
                        mqtt.listen()
                    else:
                        enroll.close()
                        server = http.server.ThreadingHTTPServer(("127.0.0.1", enroll_port), Handler)
                        threading.Thread(target=server.serve_forever, daemon=True).start()
                    output = process.communicate(timeout=12)[0]
                    assert process.returncode == 1, output
                    assert requests == ["/v1/enroll"], (requests, output)
                    assert output.index("NETWORK ready") < output.index("ENROLL requesting"), output
                    assert '"http_status":429' in output, output
                print(f"PASS: {blocked}")
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()
                mqtt.close()
                enroll.close()
                if server:
                    server.shutdown()
                    server.server_close()


if __name__ == "__main__":
    main()

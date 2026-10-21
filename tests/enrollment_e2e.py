"""Native enrollment -> private token cache -> authenticated MQTT, without real cloud calls."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "mocks"))
from common import mqtt, properties, wait_for


def main():
    with tempfile.TemporaryDirectory(prefix="agent-enrollment-") as temp:
        directory = Path(temp)
        device = "enrollment-test-device"
        key = "enr_synthetic_test_key"
        token = "lsd_synthetic_test_token"
        requests = []
        reply_mode = ["ok"]

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append((self.path, body))
                mode = reply_mode[0]
                if mode == "ok":
                    code, reply = 200, {"token": token}
                elif mode == "missing-token":
                    code, reply = 200, {}
                else:
                    code, reply = 409, {"error": {"code": mode, "message": key + token}}
                data = json.dumps(reply).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        http_thread = threading.Thread(target=http.serve_forever)
        http_thread.start()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        password_file = directory / "passwords"
        subprocess.run(["mosquitto_passwd", "-b", "-c", str(password_file), device, token], check=True)
        broker_config = directory / "mosquitto.conf"
        broker_config.write_text(chr(10).join([
            f"listener {port} 127.0.0.1", "allow_anonymous false", f"password_file {password_file}", ""]))
        processes, files = [], []
        client = None
        token_file = directory / "state/tokens" / (device + ".token")
        config = directory / "device-agent.conf"

        def write_config(explicit="", url=None):
            config.write_text(chr(10).join([
                f"host=127.0.0.1", f"port={port}", f"device={device}",
                "log-file=run/agent.log", "state-dir=state", "work-dir=work", "model=test-model", "serial=TEST-123",
                f"enroll-url={url or 'http://127.0.0.1:' + str(http.server_port) + '/v1/enroll'}",
                f"enrollment-key={key}", "allow-insecure-loopback-enroll=true", f"token={explicit}", ""]))

        def stop(proc):
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
                proc.wait(8)

        def launch():
            output = open(directory / f"native-{len(files)}.log", "w")
            files.append(output)
            proc = subprocess.Popen([str(ROOT / "build/device-agent")], cwd=directory,
                                    stdout=output, stderr=subprocess.STDOUT)
            processes.append(proc)
            wait_for(lambda: "READY" in Path(output.name).read_text() or proc.poll() is not None)
            assert proc.poll() is None, Path(output.name).read_text()
            return proc, Path(output.name)

        try:
            output = open(directory / "broker.log", "w")
            files.append(output)
            broker = subprocess.Popen(["mosquitto", "-c", str(broker_config)], stdout=output, stderr=subprocess.STDOUT)
            processes.append(broker)

            def listening():
                try:
                    with socket.create_connection(("127.0.0.1", port), .1):
                        return True
                except OSError:
                    return False

            wait_for(listening)
            subscribed = threading.Event()
            client = mqtt.Client(client_id="enrollment-test-cloud", protocol=mqtt.MQTTv5)
            client.username_pw_set(device, token)
            client.on_connect = lambda c, *args: c.subscribe(f"v1/d/{device}/up/#", qos=1)
            client.on_subscribe = lambda *args: subscribed.set()

            def received(c, u, msg):
                if msg.topic.endswith("/hello"):
                    c.publish(f"v1/d/{device}/down/hello_reply", "{}", qos=1, properties=properties(True))
            client.on_message = received
            client.connect("127.0.0.1", port)
            client.loop_start()
            assert subscribed.wait(5)
            write_config()
            agent, output = launch()
            assert requests == [("/v1/enroll", {
                "enrollment_key": key, "device_id": device, "model": "test-model", "serial": "TEST-123"})]
            assert token_file.read_text() == token
            assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
            stop(agent)
            print("PASS enrollment saves private token before authenticated JSON MQTT connection")

            reply_mode[0] = "already_enrolled"
            agent, output = launch()
            assert "loaded cached" in output.read_text()
            assert len(requests) == 1
            stop(agent)
            print("PASS restart reuses cached token without consuming enrollment key")

            token_file.write_text("")
            write_config(explicit=token)
            agent, output = launch()
            assert "using configured" in output.read_text()
            assert len(requests) == 1
            stop(agent)
            print("PASS explicit token overrides cache and enrollment")

            write_config()
            result = subprocess.run([str(ROOT / "build/device-agent")], cwd=directory,
                                    capture_output=True, text=True, timeout=5)
            assert result.returncode == 1 and "invalid token file" in (result.stdout + result.stderr)
            assert len(requests) == 1
            token_file.unlink()
            for mode, expected in (("already_enrolled", "already_enrolled"),
                                   ("invalid_enrollment_key", "invalid_enrollment_key"),
                                   ("missing-token", "no valid device token")):
                reply_mode[0] = mode
                result = subprocess.run([str(ROOT / "build/device-agent")], cwd=directory,
                                        capture_output=True, text=True, timeout=5)
                assert result.returncode == 1 and expected in (result.stdout + result.stderr), result
                assert not token_file.exists()
                assert not list(token_file.parent.glob("*.tmp.*"))
                assert key not in (result.stdout + result.stderr) and token not in (result.stdout + result.stderr)
            print("PASS rejected enrollment and corrupt cache fail clearly without leaking credentials")

            count = len(requests)
            write_config(url="http://not-a-loopback.invalid/v1/enroll")
            result = subprocess.run([str(ROOT / "build/device-agent")], cwd=directory,
                                    capture_output=True, text=True, timeout=5)
            assert result.returncode == 1 and "requires HTTPS" in (result.stdout + result.stderr)
            assert len(requests) == count
            assert key not in (directory / "run/agent.log").read_text()
            assert token not in (directory / "run/agent.log").read_text()
            print("PASS remote plaintext enrollment is rejected; logs never contain credentials")
        finally:
            for proc in reversed(processes):
                stop(proc)
            if client:
                client.disconnect()
                client.loop_stop()
            http.shutdown()
            http.server_close()
            http_thread.join()
            for output in files:
                output.close()


if __name__ == "__main__":
    main()
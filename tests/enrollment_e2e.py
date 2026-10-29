"""Native enrollment (SN + RSA-PSS signature) -> private token cache -> authenticated MQTT."""
import base64
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
import time

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "mocks"))
from common import mqtt, properties, wait_for

# 与服务端钉死的签名参数：payload 前缀、RSA-PSS、SHA-256、MGF1-SHA256、salt=32。
PAYLOAD_PREFIX = "noetix-enroll-v1|"
PSS_PADDING = padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32)


def main():
    with tempfile.TemporaryDirectory(prefix="agent-enrollment-") as temp:
        directory = Path(temp)
        device = "enrollment-test-device"
        token = "lsd_synthetic_test_token"
        # 模拟产线写入的设备密钥对；Agent 只读私钥，公钥仅测试端验签用。
        device_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        private_path = directory / "priv.pem"
        private_path.write_bytes(device_key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        private_path.chmod(0o600)
        public_pem = device_key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
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
                    # 严格验签：salt 必须等于 SHA-256 摘要长度 32，防止端侧退回
                    # PSS 最大 salt（222 字节）导致真实 equals_hash 服务端验签失败。
                    try:
                        device_key.public_key().verify(
                            base64.b64decode(body["signature"], validate=True),
                            (PAYLOAD_PREFIX + body["sn"]).encode(), PSS_PADDING, hashes.SHA256())
                    except Exception:
                        code, reply = 401, {"error": "unauthorized"}
                    else:
                        code, reply = 200, {"device_id": device, "token": token, "reissued": False}
                elif mode == "missing-token":
                    code, reply = 200, {}
                else:
                    # 扁平错误格式 {"error":"<code>"}；元组给出 (code, name)。
                    name, code = mode
                    reply = {"error": name}
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

        def write_config(explicit="", url=None, with_key=True):
            lines = [
                f"host=127.0.0.1", f"port={port}", f"device={device}",
                "log-file=run/agent.log", "state-dir=state", "work-dir=work",
                "model=test-model", "serial=TEST-123",
                f"enroll-url={url or 'http://127.0.0.1:' + str(http.server_port) + '/v1/enroll'}",
                "allow-insecure-loopback-enroll=true"]
            if with_key:
                lines.append(f"private-key={private_path}")
            if explicit:
                lines.append(f"token={explicit}")
            config.write_text(chr(10).join(lines) + chr(10))

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

        def attempt():
            # 非交互场景：Agent 必然在注册阶段退出，返回 (returncode, output)。
            result = subprocess.run([str(ROOT / "build/device-agent")], cwd=directory,
                                     capture_output=True, text=True, timeout=5)
            return result.returncode, result.stdout + result.stderr

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
            path, body = requests[-1]
            assert path == "/v1/enroll" and len(requests) == 1
            # 请求体固定两字段；model/serial 等身份信息绝不能出现在注册请求里。
            assert set(body.keys()) == {"sn", "signature"}, body
            assert body["sn"] == device
            assert len(body["signature"]) == 344  # 256 字节签名 → 标准 Base64 单行
            assert token_file.read_text() == token
            assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
            stop(agent)
            print("PASS signature enrollment saves private token before authenticated JSON MQTT connection")

            agent, output = launch()
            assert "loaded cached" in output.read_text()
            assert len(requests) == 1
            stop(agent)
            print("PASS restart reuses cached token without a new enrollment")

            token_file.write_text("")
            write_config(explicit=token)
            agent, output = launch()
            assert "using configured" in output.read_text()
            assert len(requests) == 1
            stop(agent)
            print("PASS explicit token overrides cache and enrollment")

            write_config()
            code, out = attempt()
            assert code == 1 and "invalid token file" in out
            assert len(requests) == 1
            token_file.unlink()

            for mode, expected in ((("unauthorized", 401), "unauthorized"),
                                   (("rate_limited", 429), "rate_limited"),
                                   (("sn_key_mismatch", 409), "sn_key_mismatch"),
                                   ("missing-token", "no valid device token")):
                reply_mode[0] = mode
                before = len(requests)
                code, out = attempt()
                assert code == 1 and expected in out, out
                assert len(requests) == before + 1
                assert not token_file.exists()
                assert not list(token_file.parent.glob("*.tmp.*"))
                assert token not in out and requests[-1][1]["signature"] not in out
                assert public_pem.decode() not in out
            reply_mode[0] = "ok"
            print("PASS rejected enrollments fail clearly without leaking credentials")

            before = len(requests)
            write_config(with_key=False)
            code, out = attempt()
            assert code == 1 and "set private-key and enroll-url" in out, out
            assert len(requests) == before
            print("PASS missing private key fails before any network call")

            private_path.write_text("not a pem key")
            write_config()
            code, out = attempt()
            assert code == 1 and "cannot parse private key" in out, out
            private_path.write_bytes(device_key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
            print("PASS corrupt private key fails without creating a token cache")

            # identity-dir：sn 与公钥来自 identity.json，私钥取同目录 priv.pem，无需 device/private-key。
            identity_dir = directory / "identity"
            identity_dir.mkdir()
            (identity_dir / "priv.pem").write_bytes(private_path.read_bytes())
            identity_config = chr(10).join([
                "host=127.0.0.1", f"port={port}", "log-file=run/agent.log", "state-dir=state",
                "work-dir=work", "model=test-model", "serial=TEST-123",
                f"enroll-url=http://127.0.0.1:{http.server_port}/v1/enroll",
                "allow-insecure-loopback-enroll=true", f"identity-dir={identity_dir}", ""])
            (identity_dir / "identity.json").write_text(json.dumps(
                {"sn": "identity-dir-device", "public_key": public_pem.decode()}))
            (directory / "device-agent.conf").write_text(identity_config)
            identity_token = directory / "state/tokens/identity-dir-device.token"
            before = len(requests)
            # 该身份未在 broker 注册，MQTT 不会 READY，因此以 Token 缓存出现作为 enroll 成功的判据。
            process = subprocess.Popen([str(ROOT / "build/device-agent")], cwd=directory,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            try:
                deadline = time.monotonic() + 5
                while process.poll() is None and not identity_token.exists() and time.monotonic() < deadline:
                    time.sleep(.02)
            finally:
                if process.poll() is None:
                    process.terminate()
                process.communicate(timeout=5)
            assert len(requests) == before + 1 and requests[-1][1]["sn"] == "identity-dir-device"
            assert identity_token.read_text() == token
            assert stat.S_IMODE(identity_token.stat().st_mode) == 0o600
            print("PASS identity-dir supplies sn and matching key pair without device/private-key")

            other_public = rsa.generate_private_key(public_exponent=65537, key_size=2048).public_key().public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
            identity_token.unlink()
            (identity_dir / "identity.json").write_text(json.dumps(
                {"sn": "identity-dir-device", "public_key": other_public.decode()}))
            before = len(requests)
            code, out = attempt()
            assert code == 1 and "does not match identity public_key" in out, out
            assert len(requests) == before and not identity_token.exists()
            print("PASS mismatched identity public key fails before any network call")

            identity_token.unlink(missing_ok=True)
            before = len(requests)
            write_config(url="http://not-a-loopback.invalid/v1/enroll")
            code, out = attempt()
            assert code == 1 and "requires HTTPS" in (out)
            assert len(requests) == before
            agent_log = (directory / "run/agent.log").read_text()
            assert token not in agent_log
            assert all(body["signature"] not in agent_log for _, body in requests)
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

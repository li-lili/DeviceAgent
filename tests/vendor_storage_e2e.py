"""vendor-storage identity: SN + private key from libfactory_service (mock .so).

Covers the handoff acceptance list:
1) fresh boot reads SN/privkey via the library, enrolls, caches token + sn.cache
2) valid cached token restart never calls fs_read_sn (trace only shows "loaded")
3) missing token re-reads the library on re-enroll
4) FS_ERR_ITEM_NOT_FOUND waits for provisioning without sending requests
6) missing library falls back to file identity (device-file/private-key)
"""
import base64
import json
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

ROOT = Path(__file__).resolve().parents[1]
BINARY = Path(os.environ.get("DEVICE_AGENT_TEST_BINARY", ROOT / "build/device-agent"))
MOCK_SO = ROOT / "build/libfactory_service_mock.so"

# 与服务端钉死的签名参数：payload 前缀、RSA-PSS、SHA-256、MGF1-SHA256、salt=32。
PAYLOAD_PREFIX = "noetix-enroll-v1|"
PSS_PADDING = padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32)


def main():
    assert MOCK_SO.exists(), "run scripts/test.sh first to build build/libfactory_service_mock.so"
    with tempfile.TemporaryDirectory(prefix="agent-vendor-") as temp:
        directory = Path(temp)
        device = "vendor-storage-device"
        token = "lsd_synthetic_test_token"
        device_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        key_file = directory / "vendor_priv.pem"
        key_file.write_bytes(device_key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        sn_file = directory / "vendor_sn.txt"
        sn_file.write_text(device + "\n")
        trace_file = directory / "trace.log"
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append((self.path, body))
                try:
                    device_key.public_key().verify(
                        base64.b64decode(body["signature"], validate=True),
                        (PAYLOAD_PREFIX + body["sn"]).encode(), PSS_PADDING, hashes.SHA256())
                except Exception:
                    code, reply = 401, {"error": "unauthorized"}
                else:
                    code, reply = 200, {"device_id": body["sn"], "token": token, "reissued": False}
                data = json.dumps(reply).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=http.serve_forever, daemon=True).start()
        # MQTT 用哑监听：TCP 可连但不回 CONNACK，Agent 保持在线等待，验证只关注身份链路。
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(8)
        mqtt_port = listener.getsockname()[1]
        held = []

        def accept_loop():
            while True:
                try:
                    conn, _ = listener.accept()
                except OSError:
                    return
                held.append(conn)

        threading.Thread(target=accept_loop, daemon=True).start()
        token_file = directory / "state/tokens" / (device + ".token")
        sn_cache = directory / "state/sn.cache"
        config = directory / "device-agent.conf"
        env = dict(os.environ,
                   FACTORY_MOCK_SN_FILE=str(sn_file),
                   FACTORY_MOCK_KEY_FILE=str(key_file),
                   FACTORY_MOCK_TRACE=str(trace_file))
        # MQTT 指向哑监听端口：连上后等 CONNACK，不影响身份与 enroll 断言。
        base = [
            "host=127.0.0.1", f"port={mqtt_port}", "log-file=run/agent.log", "state-dir=state", "work-dir=work",
            "model=test-model", "serial=TEST-123",
            f"enroll-url=http://127.0.0.1:{http.server_port}/v1/enroll",
            "allow-insecure-loopback-enroll=true"]

        def write_config(vendor=True):
            lines = list(base)
            if vendor:
                lines += ["identity-source=vendor-storage", f"factory-lib={MOCK_SO}"]
            else:
                lines += [f"device-file={sn_file}", f"private-key={key_file}"]
            config.write_text(chr(10).join(lines) + chr(10))

        def launch():
            trace_file.write_text("")
            output = directory / f"agent-{time.monotonic_ns()}.log"
            with open(output, "w") as sink:
                proc = subprocess.Popen([str(BINARY)], cwd=directory,
                                        stdout=sink, stderr=subprocess.STDOUT, env=env)
            return proc, output

        def wait_token(proc, output, timeout=5):
            deadline = time.monotonic() + timeout
            while proc.poll() is None and not token_file.exists() and time.monotonic() < deadline:
                time.sleep(.02)
            assert token_file.exists(), output.read_text()

        def stop(proc):
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
                proc.wait(8)

        proc = None
        try:
            # 验收 1：首次启动经库读 SN/私钥，enroll 成功，token 与 sn.cache 落盘。
            write_config()
            proc, output = launch()
            wait_token(proc, output)
            path, body = requests[-1]
            assert path == "/v1/enroll" and set(body.keys()) == {"sn", "signature"}, body
            assert body["sn"] == device
            assert token_file.read_text() == token
            import stat
            assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
            assert sn_cache.read_text() == device
            assert stat.S_IMODE(sn_cache.stat().st_mode) == 0o600
            # 首次启动 fs_read_sn 调用两次：load_device 首读（无缓存）+ enroll 前一致性重读。
            assert trace_file.read_text().splitlines() == ["loaded", "sn", "sn", "privkey"], trace_file.read_text()
            stop(proc)
            print("PASS vendor-storage enroll reads SN and key via the library")

            # 验收 2：token 有效时重启只加载库，不调用 fs_read_sn。
            before = len(requests)
            proc, output = launch()
            time.sleep(2.5)
            assert proc.poll() is None, output.read_text()
            text = output.read_text()
            assert "FATAL" not in text and "loaded cached" in text, text
            assert trace_file.read_text().splitlines() == ["loaded"], trace_file.read_text()
            assert len(requests) == before
            stop(proc)
            print("PASS valid cached token restart never touches vendor storage reads")

            # 验收 3：token 失效后重新 enroll，重新读库。
            token_file.unlink()
            proc, output = launch()
            wait_token(proc, output)
            assert trace_file.read_text().splitlines() == ["loaded", "sn", "privkey"], trace_file.read_text()
            assert requests[-1][1]["sn"] == device and token_file.read_text() == token
            stop(proc)
            print("PASS missing token re-reads vendor storage before re-enroll")

            # 验收 4：Vendor Storage 未烧写 SN 时保持运行，不调用注册接口。
            env["FACTORY_MOCK_STATUS"] = "-7"
            shutil.rmtree(directory / "state", ignore_errors=True)
            before = len(requests)
            proc, output = launch()
            time.sleep(1)
            assert proc.poll() is None, output.read_text()
            assert "IDENTITY waiting item=SN" in output.read_text()
            assert len(requests) == before
            assert not (directory / "state/tokens").exists()
            stop(proc)
            del env["FACTORY_MOCK_STATUS"]
            print("PASS unprovisioned vendor storage waits without enrollment")

            # 验收 6：库缺失时回退 file 身份来源（device-file/private-key）。
            config.write_text(chr(10).join(
                base + [f"device-file={sn_file}", f"private-key={key_file}",
                        "identity-source=vendor-storage", "factory-lib=/nonexistent/libfactory_service.so.1"]) + chr(10))
            shutil.rmtree(directory / "state", ignore_errors=True)
            proc, output = launch()
            wait_token(proc, output)
            text = output.read_text()
            assert "falling back to file identity" in text, text
            assert requests[-1][1]["sn"] == device and token_file.read_text() == token
            stop(proc)
            print("PASS missing library falls back to file identity")
        finally:
            if proc and proc.poll() is None:
                stop(proc)
            for conn in held:
                conn.close()
            listener.close()
            http.shutdown()
            http.server_close()


if __name__ == "__main__":
    main()

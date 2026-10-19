"""Mock cloud + loopback multipart object storage. No real cloud credentials used."""
import argparse
from collections import deque
import queue
from datetime import datetime, timedelta
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
from pathlib import Path
import secrets
import shlex
import threading
import time
import uuid
from zoneinfo import ZoneInfo
from common import cloud, mqtt, publish, wait_for, parse_cloud


class CloudSimulator:
    def __init__(self, port, device, storage, http_port=0, host="127.0.0.1", on_event=None):
        self.device = device
        self.storage = Path(storage).resolve()
        self.storage.mkdir(parents=True, exist_ok=True)
        self.up = f"v1/d/{device}/up/"
        self.down = f"v1/d/{device}/down/"
        self.connected = threading.Event()
        self.ready = threading.Event()
        self.handshakes = queue.Queue()
        self.pending_handshakes = deque()
        self.results = {}
        self.requests = {}
        self.grants = {}
        self.uploads = {}
        self.verified = {}
        self.errors = []
        self.on_event = on_event
        self.deny_upload = False
        self.defer_grants = False
        self.lock = threading.RLock()
        owner = self

        class StorageHandler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                try:
                    length = int(self.headers.get("Content-Length", 0))
                    if self.path != "/upload" or not 0 < length <= 130 * 1024 * 1024:
                        self.send_error(413)
                        return
                    payload = self.rfile.read(length)
                    headers = ("Content-Type: " + self.headers.get("Content-Type", "") +
                               "\r\nMIME-Version: 1.0\r\n\r\n").encode()
                    multipart = BytesParser(policy=default).parsebytes(headers + payload)
                    values = {part.get_param("name", header="content-disposition"): part.get_payload(decode=True)
                              for part in multipart.iter_parts()}
                    token = values["token"].decode()
                    key = values["key"].decode()
                    data = values["file"]
                    with owner.lock:
                        grant = next((g for g in owner.grants.values()
                                      if g.uptoken == token and g.storage_key == key), None)
                        if not grant or grant.expires_at_ns <= time.time_ns() or len(data) > grant.max_size_bytes:
                            self.send_error(403)
                            return
                        req = owner.requests[grant.command_id]
                        if len(data) != req.size_bytes or hashlib.sha256(data).hexdigest() != req.sha256:
                            self.send_error(400, "size or digest mismatch")
                            return
                        destination = owner.storage / (grant.file_id + ".tar.gz")
                        owner.storage.mkdir(parents=True, exist_ok=True)
                        destination.write_bytes(data)
                        owner.uploads[grant.command_id] = destination
                    if owner.on_event:
                        owner.on_event("文件服务收到上传", {"file_id": grant.file_id,
                            "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                            "size_and_sha256_verified": True, "saved_to": str(destination)})
                    response = json.dumps({"key": key}).encode()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(response)))
                    self.end_headers()
                    self.wfile.write(response)
                except Exception as exc:
                    owner.errors.append(str(exc))
                    self.send_error(400)

        self.http = ThreadingHTTPServer(("127.0.0.1", http_port), StorageHandler)
        self.http_thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.http_thread.start()
        self.client = mqtt.Client(client_id=f"mock-cloud-{uuid.uuid4().hex[:8]}", protocol=mqtt.MQTTv5)

        def connected(client, userdata, flags, reason, props):
            if reason == 0:
                client.subscribe(self.up + "#", qos=1)

        def subscribed(client, userdata, mid, codes, props):
            self.connected.set()

        def received(client, userdata, msg):
            try:
                self.on_message(msg)
            except Exception as exc:
                self.errors.append(str(exc))

        self.client.on_connect = connected
        self.client.on_subscribe = subscribed
        self.client.on_message = received
        self.client.connect(host, port, 30)
        self.client.loop_start()
        if not self.connected.wait(5):
            raise TimeoutError("cloud MQTT subscription")

    def on_message(self, msg):
        # Cloud MQTT uses JSON with lowerCamelCase names, string int64 values and named enums.
        user = dict(getattr(msg.properties, "UserProperty", []) or [])
        if user.get("proto") != "1" or user.get("ct") != "json":
            raise ValueError("expected JSON MQTT properties proto=1 and ct=json")
        kind = msg.topic.rsplit("/", 1)[-1]
        if kind == "hello":
            hello = cloud.Hello()
            parse_cloud(msg.payload, hello)
            if hello.device_id != self.device:
                raise ValueError("wrong device")
            with self.lock:
                self.pending_handshakes.append(hello.boot_id)
            publish(self.client, self.down + "hello_reply", cloud.HelloReply(server_time_ns=time.time_ns()))
        elif kind == "inventory":
            inventory = cloud.Inventory()
            parse_cloud(msg.payload, inventory)
            if inventory.device_id != self.device:
                raise ValueError("wrong inventory device")
            self.ready.set()
            with self.lock:
                if self.pending_handshakes:
                    self.handshakes.put(self.pending_handshakes.popleft())
        elif kind == "upload_request":
            request = cloud.UploadRequest()
            parse_cloud(msg.payload, request)
            with self.lock:
                self.requests[request.command_id] = request
            if not self.defer_grants:
                self.send_grant(request.command_id)
        elif kind == "cmd_result":
            result = cloud.CommandResult()
            parse_cloud(msg.payload, result)
            with self.lock:
                self.results.setdefault(result.command_id, []).append(result)
                if result.status == cloud.CMD_SUCCEEDED:
                    path = self.uploads.get(result.command_id)
                    expected = result.collect_logs
                    if not path or path.stat().st_size != expected.size_bytes or hashlib.sha256(path.read_bytes()).hexdigest() != expected.sha256:
                        raise ValueError("cloud object verification failed")
                    self.verified[result.command_id] = path

    def send_grant(self, command_id):
        with self.lock:
            if command_id not in self.grants:
                file_id = uuid.uuid4().hex
                self.grants[command_id] = cloud.UploadGrant(
                    command_id=command_id, granted=not self.deny_upload,
                    reject_reason="mock denial" if self.deny_upload else "",
                    file_id=file_id, storage_key=f"files/{self.device}/{file_id}",
                    uptoken=secrets.token_urlsafe(24),
                    up_hosts=[f"http://127.0.0.1:{self.http.server_port}/upload"],
                    expires_at_ns=time.time_ns() + 3600 * 10**9,
                    max_size_bytes=self.requests[command_id].size_bytes)
            grant = self.grants[command_id]
        publish(self.client, self.down + "upload_grant", grant)

    def make_command(self, mode="latest", sources=(), start=None, end=None, tz="Asia/Shanghai", command_id=None):
        now = time.time_ns()
        command = cloud.Command(command_id=command_id or uuid.uuid4().hex,
                                issued_at_ns=now, expires_at_ns=now + 3600 * 10**9)
        args = command.collect_logs
        args.sources.extend(sources)
        args.max_bundle_bytes = 16 * 1024 * 1024
        if mode == "latest":
            args.selection_mode = cloud.LATEST_FILE
        else:
            zone = ZoneInfo(tz)
            start = start or datetime.now(zone).strftime("%Y-%m-%d")
            first = datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=zone)
            last = datetime.strptime(end or start, "%Y-%m-%d").replace(tzinfo=zone) + timedelta(days=1)
            if last <= first:
                raise ValueError("end date must be on or after start date")
            args.selection_mode = cloud.TIME_RANGE
            args.time_from_ns = int(first.timestamp()) * 10**9
            args.time_to_ns = int(last.timestamp()) * 10**9
        return command

    def send(self, command):
        publish(self.client, self.down + "cmd", command)

    def terminal(self, command_id, timeout=20, cancel=None):
        def found():
            if cancel is not None and cancel.is_set():
                raise InterruptedError("cloud is stopping")
            with self.lock:
                values = self.results.get(command_id, [])
                terminal = [r for r in values if r.status in (cloud.CMD_SUCCEEDED, cloud.CMD_FAILED, cloud.CMD_REJECTED)]
                return terminal[-1] if terminal else None
        return wait_for(found, timeout, f"command {command_id}")

    def close(self):
        self.client.disconnect()
        self.client.loop_stop()
        self.http.shutdown()
        self.http.server_close()
        self.http_thread.join()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=18883)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--device", default="demo-device")
    p.add_argument("--storage", default="run/cloud")
    p.add_argument("--http-port", type=int, default=18080)
    p.add_argument("--mode", choices=["latest", "days"], default="days")
    p.add_argument("--start", help="inclusive YYYY-MM-DD; default=today in Asia/Shanghai")
    p.add_argument("--end", help="inclusive YYYY-MM-DD; default=start")
    p.add_argument("--sources", nargs="*", default=[])
    p.add_argument("--once", action="store_true", help="send one command and exit")
    args = p.parse_args()
    sim = CloudSimulator(args.port, args.device, args.storage, args.http_port, args.host)
    stop = threading.Event()
    collect_lock = threading.Lock()
    worker = None

    def collect(mode, sources, start=None, end=None):
        # Serialize automatic and manual requests; Agent accepts one task at a time.
        with collect_lock:
            if stop.is_set():
                raise InterruptedError("cloud is stopping")
            command = sim.make_command(mode, sources, start, end)
            print("Sending command:", command.command_id, flush=True)
            if command.collect_logs.selection_mode == cloud.TIME_RANGE:
                zone = ZoneInfo("Asia/Shanghai")
                first = datetime.fromtimestamp(command.collect_logs.time_from_ns / 10**9, zone)
                last = datetime.fromtimestamp(command.collect_logs.time_to_ns / 10**9, zone)
                print(f"Log range: {first.isoformat()} <= time < {last.isoformat()}", flush=True)
            sim.send(command)
            result = sim.terminal(command.command_id, cancel=stop)
            print(result, flush=True)
            if command.command_id in sim.verified:
                print("Verified log bundle:", sim.verified[command.command_id], flush=True)
            return result.status == cloud.CMD_SUCCEEDED

    def auto_collect():
        while not stop.is_set():
            try:
                boot = sim.handshakes.get(timeout=.2)
            except queue.Empty:
                continue
            print("Agent handshake complete; automatically collecting logs. boot_id=" + boot, flush=True)
            # Preserve the grace period for retained MQTT source registrations.
            if stop.wait(1):
                return
            try:
                collect(args.mode, args.sources, args.start, args.end)
            except InterruptedError:
                return
            except Exception as exc:
                print("Automatic command error:", exc, flush=True)

    try:
        print("Waiting for Agent hello; each completed handshake triggers a collection.", flush=True)
        if args.once:
            try:
                sim.handshakes.get(timeout=60)
            except queue.Empty:
                raise TimeoutError("Agent handshake")
            time.sleep(1)
            if not collect(args.mode, args.sources, args.start, args.end):
                raise SystemExit(1)
            return
        worker = threading.Thread(target=auto_collect, name="cloud-auto-collect")
        worker.start()
        print("Cloud stays running. Commands:", flush=True)
        print("  today [source ...]  (today in Asia/Shanghai)", flush=True)
        print("  latest [source ...]", flush=True)
        print("  days YYYY-MM-DD [YYYY-MM-DD] [source ...]", flush=True)
        print("  quit  (or Ctrl+C)", flush=True)
        while True:
            try:
                parts = shlex.split(input("cloud> "))
                if not parts:
                    continue
                if parts[0] in ("quit", "exit"):
                    break
                if parts[0] == "today" or parts == ["days"]:
                    collect("days", parts[1:] or args.sources)
                elif parts[0] == "latest":
                    collect("latest", parts[1:] or args.sources)
                elif parts[0] == "days" and len(parts) >= 2:
                    start = parts[1]
                    datetime.strptime(start, "%Y-%m-%d")
                    end = start
                    sources = parts[2:]
                    if sources:
                        try:
                            datetime.strptime(sources[0], "%Y-%m-%d")
                        except ValueError:
                            pass
                        else:
                            end, sources = sources[0], sources[1:]
                    if end < start:
                        raise ValueError("end date must be on or after start date")
                    collect("days", sources or args.sources, start, end)
                else:
                    print("Use: today [source ...] | latest [source ...] | days START [END] [source ...] | quit", flush=True)
            except EOFError:
                print("Standard input closed; cloud remains running until interrupted.", flush=True)
                stop.wait()
            except (ValueError, TimeoutError) as exc:
                print("Command error:", exc, flush=True)
    except KeyboardInterrupt:
        print("\nStopping cloud.", flush=True)
    finally:
        stop.set()
        if worker is not None:
            worker.join()
        sim.close()

if __name__ == "__main__":
    main()

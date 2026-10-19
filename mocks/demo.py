"""Guided real MQTT/JSON demonstration. --step pauses before each stage."""
import argparse
from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import tarfile
import threading
import time
import uuid
from zoneinfo import ZoneInfo
from google.protobuf.json_format import MessageToDict
from common import BusinessSimulator, business, cloud, mqtt, wait_for, parse_cloud
from business import make_registration
from cloud import CloudSimulator

ROOT = Path(__file__).resolve().parents[1]


class Timeline:
    def __init__(self, directory, step):
        self.directory, self.step = directory, step
        self.lock = threading.Lock()
        self.entries = []
        self.file = (directory / "process.log").open("w", encoding="utf-8")

    def emit(self, title, content=None):
        stamp = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%H:%M:%S.%f")[:-3]
        detail = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, indent=2) if content is not None else ""
        with self.lock:
            self.entries.append((stamp, title, detail))
            text = f"[{stamp}] {title}\n" + (detail + "\n" if detail else "")
            print(text, flush=True)
            self.file.write(text + "\n")
            self.file.flush()

    def stage(self, title):
        if self.step:
            input(f"\n按回车继续：{title} … ")
        self.emit("阶段：" + title)

    def save(self, success):
        lines = ["# 取日志全过程演示", "", f"结果：{'成功' if success else '未完成'}", "",
                 "云端 MQTT 实际传输 JSON，兼容业务注册仍传输 PB；观察器展示真实消息，上传凭证已隐藏。", "",
                 "本次使用本机 MQTT 和 HTTP 模拟对象存储，不是生产云端或 HTTPS 联调。", ""]
        with self.lock:
            for stamp, title, content in self.entries:
                lines += [f"### {stamp} · {title}", ""]
                if content:
                    fence = "````" if "```" in content else "```"
                    lines += [fence + ("json" if content.startswith("{") else "text"), content, fence, ""]
        text = "\n".join(lines)
        (self.directory / "process.md").write_text(text, encoding="utf-8")
        (ROOT / "run/latest-demo.md").write_text(text, encoding="utf-8")
        self.file.close()


class Observer:
    def __init__(self, port, device, timeline):
        self.timeline = timeline
        self.ready = threading.Event()
        self.client = mqtt.Client(client_id="demo-observer-" + uuid.uuid4().hex[:8], protocol=mqtt.MQTTv5)
        self.client.on_connect = lambda c, u, f, r, p: c.subscribe(
            [(f"local/v1/{device}/biz/#", 1), (f"v1/d/{device}/#", 1)]) if r == 0 else None
        self.client.on_subscribe = lambda *args: self.ready.set()
        self.client.on_message = self.received
        self.client.connect("127.0.0.1", port, 30)
        self.client.loop_start()
        if not self.ready.wait(5):
            raise TimeoutError("MQTT observer subscription")

    def received(self, client, userdata, msg):
        kind = msg.topic.rsplit("/", 1)[-1]
        types = {"register": business.Registration, "register_ack": business.RegistrationAck,
                 "presence": cloud.Presence, "hello": cloud.Hello,
                 "hello_reply": cloud.HelloReply, "inventory": cloud.Inventory,
                 "cmd": cloud.Command, "cmd_result": cloud.CommandResult,
                 "upload_request": cloud.UploadRequest, "upload_grant": cloud.UploadGrant}
        if kind not in types:
            return
        try:
            value = types[kind]()
            json_wire = not msg.topic.startswith("local/")
            if json_wire:
                parse_cloud(msg.payload, value)
            else:
                value.ParseFromString(msg.payload)
            payload = MessageToDict(value, preserving_proto_field_name=False)
            if "uptoken" in payload:
                payload["uptoken"] = "<已隐藏>"
            direction = "业务 → Agent" if kind == "register" else "Agent → 业务" if kind == "register_ack" else "云端 → Agent" if "/down/" in msg.topic else "Agent → 云端"
            self.timeline.emit(f"MQTT 实收：{direction} · {kind}",
                {"topic": msg.topic, "encoding": "JSON" if json_wire else "Protobuf", "wire_bytes": len(msg.payload), "payload": payload})
        except Exception as exc:
            self.timeline.emit("观察器解码失败", str(exc))

    def close(self):
        self.client.disconnect()
        self.client.loop_stop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--step", action="store_true", help="每阶段按回车继续")
    args = parser.parse_args()
    directory = ROOT / "run" / ("demo-" + str(time.time_ns()))
    directory.mkdir(parents=True)
    timeline = Timeline(directory, args.step)
    handles, processes = [], []
    observer = sim = biz = None
    thread = None
    success = False
    try:
        timeline.stage("准备业务原始日志")
        day = datetime.now(ZoneInfo("Asia/Shanghai")).date()
        previous = day - timedelta(days=1)
        log_root = directory / "logs"
        contents = {
            f"app/{previous}.log": f"{previous} 08:00:00.000[INFO]: yesterday app started\n{previous} 09:00:00.123[ERROR]: yesterday camera timeout\n",
            f"app/{day}.log": f"{day} 10:00:00.000[INFO]: today app started\n{day} 10:01:00.456[ERROR]: today camera disconnected\n",
            f"sdk/{previous}.log": f"{previous} 09:00:00.123[ERROR]: yesterday SDK timeout\n",
            f"sdk/{day}.log": f"{day} 10:01:00.456[ERROR]: today SDK disconnected\n",
        }
        for index, (name, content) in enumerate(contents.items()):
            file = log_root / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(content, encoding="utf-8")
            os.utime(file, (time.time() - 100 + index, time.time() - 100 + index))
            timeline.emit("业务原始文件：" + name, content)

        timeline.stage("启动本机 MQTT、模拟云端和消息观察器")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        config = directory / "mosquitto.conf"
        config.write_text(f"listener {port} 127.0.0.1\nallow_anonymous true\npersistence false\n")
        broker_log = open(directory / "broker.log", "w")
        handles.append(broker_log)
        broker = subprocess.Popen(["mosquitto", "-c", str(config)], stdout=broker_log, stderr=subprocess.STDOUT)
        processes.append(broker)

        def listening():
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=.1):
                    return True
            except OSError:
                return False
        wait_for(listening, description="MQTT broker")
        observer = Observer(port, "demo-device", timeline)
        sim = CloudSimulator(port, "demo-device", directory / "cloud", on_event=timeline.emit)
        timeline.emit("服务已就绪", {"mqtt": f"127.0.0.1:{port}",
            "模拟文件服务": f"http://127.0.0.1:{sim.http.server_port}/upload"})

        timeline.stage("业务发送日志注册配置")
        biz = BusinessSimulator(port, "demo-device", make_registration(log_root))
        timeline.emit("业务已通过 MQTT 发布 retained PB 注册；Agent 随后启动接收")

        timeline.stage("启动 C++ Agent，完成注册确认与云端握手")
        (directory / "device-agent.conf").write_text(chr(10).join([
            f"port={port}", f"allowed-root={log_root}", f"state-dir={directory / 'agent-state'}",
            f"work-dir={directory / 'work'}", "sources-config=", "allow-insecure-loopback-upload=true", ""]))
        agent = subprocess.Popen([str(ROOT / "build/device-agent")],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, cwd=directory)
        processes.append(agent)

        def agent_output():
            with open(directory / "agent.log", "w") as out:
                for line in agent.stdout:
                    out.write(line)
                    out.flush()
                    timeline.emit("Agent 执行", line.rstrip())
        thread = threading.Thread(target=agent_output, daemon=True)
        thread.start()
        wait_for(lambda: sim.ready.is_set() and biz.acks, description="registration and hello")
        if not biz.acks[-1].accepted:
            raise RuntimeError(biz.acks[-1].message)

        for mode in ("latest", "days"):
            title = "云端请求：各来源最新文件" if mode == "latest" else f"云端请求：{previous} 当天日志"
            timeline.stage(title)
            cmd = sim.make_command(mode, sources=["business", "camera_sdk"], start=str(previous))
            sim.send(cmd)
            result = sim.terminal(cmd.command_id)
            if result.status != cloud.CMD_SUCCEEDED:
                raise RuntimeError(result)
            path = sim.verified[cmd.command_id]
            timeline.emit("云端校验完成，日志包可下载", {"command_id": cmd.command_id,
                "file_id": result.collect_logs.file_id, "bundle": str(path), "size_bytes": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
            with tarfile.open(path) as archive:
                for member in archive:
                    if not member.isfile():
                        continue
                    data = archive.extractfile(member).read()
                    if member.name == "manifest.json":
                        timeline.emit("云端查看包内清单", json.loads(data))
                    else:
                        text = data.decode("utf-8")
                        if mode == "days" and "today" in text:
                            raise AssertionError("day selection leaked today's records")
                        timeline.emit("云端查看日志：" + member.name, text)
            wait_for(lambda: not list((directory / "work").iterdir()), description="task cleanup")

        if sim.errors:
            raise RuntimeError(sim.errors)
        success = True
        timeline.emit("全过程完成", "两种取日志方式均完成真实 MQTT/JSON、C++ 文件处理、HTTP 上传和云端校验。")
    finally:
        if biz:
            biz.close()
        for proc in reversed(processes[1:]):
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
                try:
                    proc.wait(8)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
        if thread:
            thread.join(timeout=2)
        if sim:
            sim.close()
        if observer:
            observer.close()
        for proc in processes[:1]:
            proc.terminate()
            proc.wait(5)
        for handle in handles:
            handle.close()
        timeline.emit("演示进程已关闭，产物已保留", str(directory))
        timeline.save(success)
        print("\n全过程报告：", ROOT / "run/latest-demo.md", flush=True)


if __name__ == "__main__":
    main()

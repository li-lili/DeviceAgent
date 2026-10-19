"""Verify file-only sources with real MQTT, Agent and upload storage."""
import json
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "mocks"))
from common import BusinessSimulator, business, cloud, mqtt, wait_for
from cloud import CloudSimulator


def main():
    with tempfile.TemporaryDirectory(prefix="device-agent-sources-") as temp:
        directory = Path(temp)
        logs = directory / "logs"
        (logs / "app").mkdir(parents=True)
        expected = b"2026-10-16 12:00:00[INFO]: file configuration test" + bytes([10])
        (logs / "app/current.log").write_bytes(expected)
        sources = directory / "sources.json"

        def write_sources(source_id="business", path="logs//app"):
            sources.write_text("/* sources config */" + chr(10) + "// line comment" + chr(10) + json.dumps({"log_sources": [{
                "source_id": source_id, "path": path, "recursive": True,
                "file_patterns": ["*.log"]}]}) + chr(10) + "// trailing comment")

        write_sources()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        broker_config = directory / "mosquitto.conf"
        broker_config.write_text(chr(10).join([
            f"listener {port} 127.0.0.1", "allow_anonymous true", "persistence false", ""]))
        config = directory / "device-agent.conf"
        profiles = directory / "profiles"
        profiles.mkdir()
        (profiles / "local.conf").write_text(chr(10).join([
            f"port={port}", "allowed-root=../logs", "state-dir=../state", "work-dir=../work",
            "sources-config=../sources.json", "allow-insecure-loopback-upload=true", ""]))
        config.write_text("config-file=profiles/local.conf" + chr(10))
        cache = directory / "state/registrations"
        cache.mkdir(parents=True)
        stale = business.Registration(business_id="old", config_version=1)
        stale.log_sources.add(source_id="cached", path=str(logs / "app"))
        (cache / "old.pb").write_bytes(stale.SerializeToString())
        processes, outputs = [], []
        sim = observer = None
        wire = []

        def stop(proc):
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
                proc.wait(8)

        def launch():
            output = open(directory / f"agent-{len(outputs)}.log", "w")
            outputs.append(output)
            proc = subprocess.Popen([str(ROOT / "build/device-agent")],
                                    cwd=directory, stdout=output, stderr=subprocess.STDOUT)
            processes.append(proc)
            wait_for(lambda: "READY" in Path(output.name).read_text() or proc.poll() is not None)
            assert proc.poll() is None, Path(output.name).read_text()
            return proc

        try:
            output = open(directory / "broker.log", "w")
            outputs.append(output)
            broker = subprocess.Popen(["mosquitto", "-c", str(broker_config)], stdout=output, stderr=subprocess.STDOUT)
            processes.append(broker)

            def listening():
                try:
                    with socket.create_connection(("127.0.0.1", port), .1):
                        return True
                except OSError:
                    return False

            wait_for(listening)
            sim = CloudSimulator(port, "demo-device", directory / "cloud")
            subscribed = threading.Event()
            observer = mqtt.Client(client_id="json-wire-observer", protocol=mqtt.MQTTv5)
            observer.on_connect = lambda c, *args: c.subscribe("v1/d/demo-device/#", qos=1)
            observer.on_subscribe = lambda *args: subscribed.set()
            def observe(c, u, msg):
                try:
                    body = json.loads(msg.payload.decode())
                except (ValueError, UnicodeError):
                    return  # The malformed-command test is intentionally not JSON.
                wire.append((msg.topic.rsplit("/", 1)[-1], body,
                             dict(getattr(msg.properties, "UserProperty", []) or [])))
            observer.on_message = observe
            observer.connect("127.0.0.1", port)
            observer.loop_start()
            assert subscribed.wait(5)
            agent = launch()
            sim.storage.rmdir()  # Upload must recreate a deleted storage directory.
            command = sim.make_command(sources=["business"])
            sim.send(command)
            result = sim.terminal(command.command_id)
            assert result.status == cloud.CMD_SUCCEEDED, result
            with tarfile.open(sim.verified[command.command_id]) as tar:
                assert tar.extractfile("business/current.log").read() == expected
            print("PASS file-only startup and verified upload without business publisher")
            # Exercise the native parser using hand-written JSON, without protobuf encoding.
            from paho.mqtt.properties import Properties
            from paho.mqtt.packettypes import PacketTypes
            props = Properties(PacketTypes.PUBLISH)
            props.UserProperty = [("proto", "1"), ("ct", "json")]
            observer.publish("v1/d/demo-device/down/cmd", "{", qos=1, properties=props)
            command_id = uuid.uuid4().hex
            raw_command = {
                "commandId": command_id, "issuedAtNs": str(time.time_ns()),
                "expiresAtNs": str(time.time_ns() + 60 * 10**9),
                "futureEnvelopeField": "ignored",
                "collectLogs": {"selectionMode": "LATEST_FILE", "sources": ["business"],
                                "maxBundleBytes": "16777216", "futureArgument": True}}
            observer.publish("v1/d/demo-device/down/cmd", json.dumps(raw_command), qos=1, properties=props)
            assert sim.terminal(command_id).status == cloud.CMD_SUCCEEDED
            wait_for(lambda: any(kind == "cmd_result" and body.get("commandId") == command_id and
                                 body.get("status") == "CMD_SUCCEEDED" for kind, body, _ in wire))
            assert all(props.get("ct") == "json" and props.get("proto") == "1" for _, _, props in wire)
            hello = next(body for kind, body, _ in wire if kind == "hello")
            assert hello["deviceId"] == "demo-device" and "device_id" not in hello
            assert isinstance(hello["sentWallNs"], str) and int(hello["sentWallNs"]) > 2**53
            uploaded = next(body for kind, body, _ in wire if kind == "upload_request")
            assert isinstance(uploaded["sizeBytes"], str) and isinstance(uploaded["sha256"], str)
            print("PASS raw JSON commands, forward-compatible fields, int64 strings and named enums")


            registration = business.Registration(business_id="injected", config_version=1)
            registration.log_sources.add(source_id="injected", path=str(logs / "app"))
            biz = BusinessSimulator(port, "demo-device", registration)
            biz.close()
            for source_id in ("cached", "injected"):
                command = sim.make_command(sources=[source_id])
                sim.send(command)
                assert sim.terminal(command.command_id).error_code == "UNKNOWN_SOURCE"
            print("PASS file configuration ignores old cache and MQTT registrations")

            stop(agent)
            write_sources("renamed")
            agent = launch()
            for source_id, expected_status in (("renamed", cloud.CMD_SUCCEEDED), ("business", cloud.CMD_FAILED)):
                command = sim.make_command(sources=[source_id])
                sim.send(command)
                assert sim.terminal(command.command_id).status == expected_status
            print("PASS restart reloads edited file and removes previous source")

            # SIGKILL forces the broker to publish the native client's JSON last will.
            agent.kill()
            agent.wait(5)
            wait_for(lambda: any(kind == "presence" and not body.get("online", False)
                                 for kind, body, _ in wire), description="JSON last will")
            assert all(props.get("ct") == "json" and props.get("proto") == "1" for _, _, props in wire)
            assert not sim.errors, sim.errors
            print("PASS JSON last will; profile selection resolves paths from selected config")
            valid = {"source_id": "x", "path": "logs/app"}
            invalid = [
                json.dumps({"log_sources": [dict(valid, path="/etc")]}),
                '{"log_sources":[]}', '{broken', '/* unclosed',
                json.dumps({"log_sources": [valid, valid]})]
            for text in invalid:
                sources.write_text(text)
                result = subprocess.run([str(ROOT / "build/device-agent")],
                                        cwd=directory, capture_output=True, text=True, timeout=5)
                assert result.returncode == 1 and "FATAL" in result.stdout, result
            print("PASS outside-root, empty, malformed and duplicate source configurations fail startup")
        finally:
            for proc in reversed(processes):
                stop(proc)
            if observer:
                observer.disconnect()
                observer.loop_stop()
            if sim:
                sim.close()
            for output in outputs:
                output.close()


if __name__ == "__main__":
    main()
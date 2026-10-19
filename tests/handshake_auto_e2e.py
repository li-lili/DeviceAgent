"""Real persistent CLI: automatic requests after every Agent handshake."""
from datetime import datetime
import json
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "mocks"))
from common import wait_for


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main():
    with tempfile.TemporaryDirectory(prefix="cloud-handshake-") as temp:
        directory = Path(temp)
        logs = directory / "logs"
        logs.mkdir()
        broker_config = directory / "mosquitto.conf"
        port = free_port()
        broker_config.write_text(chr(10).join([
            f"listener {port} 127.0.0.1", "allow_anonymous true", "persistence false", ""]))
        sources = directory / "sources.json"
        sources.write_text(json.dumps({"log_sources": [{
            "source_id": "business", "path": "logs", "recursive": True,
            "file_patterns": ["*.log"]}]}))
        config = directory / "device-agent.conf"
        config.write_text(chr(10).join([
            f"port={port}", "allowed-root=logs", "state-dir=state", "work-dir=work",
            "sources-config=sources.json", "allow-insecure-loopback-upload=true", ""]))
        outputs, processes = [], []

        def start(args, name, interactive=False):
            output = open(directory / name, "w")
            outputs.append(output)
            proc = subprocess.Popen(args, cwd=directory, stdout=output, stderr=subprocess.STDOUT,
                                    stdin=subprocess.PIPE if interactive else subprocess.DEVNULL, text=True)
            processes.append(proc)
            return proc

        def stop(proc):
            if proc.poll() is None:
                proc.send_signal(signal.SIGINT)
                proc.wait(8)

        try:
            start(["mosquitto", "-c", str(broker_config)], "broker.log")

            def listening():
                try:
                    with socket.create_connection(("127.0.0.1", port), .1):
                        return True
                except OSError:
                    return False

            wait_for(listening)
            sim = start(["/usr/bin/python3", str(ROOT / "mocks/cloud.py"),
                         "--port", str(port), "--http-port", "0"], "cloud.log", True)
            output = directory / "cloud.log"
            wait_for(lambda: "Cloud stays running" in output.read_text())
            agent = start([str(ROOT / "build/device-agent")], "agent-1.log")
            wait_for(lambda: "NO_LOGS_IN_RANGE" in output.read_text())
            assert sim.poll() is None
            stop(agent)
            print("PASS failed automatic collection leaves cloud running")

            content = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")
            content += "[INFO]: automatic collection after handshake" + chr(10)
            (logs / (datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d") + ".log")).write_text(content)
            for index in (2, 3):
                agent = start([str(ROOT / "build/device-agent")], f"agent-{index}.log")
                wait_for(lambda: output.read_text().count("Verified log bundle:") == index - 1,
                         timeout=12, description="automatic handshake upload")
                assert sim.poll() is None, output.read_text()
                stop(agent)
            assert output.read_text().count("automatically collecting logs") == 3
            bundles = list((directory / "run/cloud").glob("*.tar.gz"))
            assert len(bundles) == 2
            for bundle in bundles:
                with tarfile.open(bundle) as tar:
                    assert tar.extractfile("business/" + datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d") + ".log").read() == content.encode()
            print("PASS every completed handshake triggers exactly one new verified daily log bundle")

            # Keep this Agent alive, then request another bundle through the same CLI.
            agent = start([str(ROOT / "build/device-agent")], "agent-4.log")
            wait_for(lambda: output.read_text().count("Verified log bundle:") == 3, timeout=12)
            sim.stdin.write("today business" + chr(10))
            sim.stdin.flush()
            wait_for(lambda: output.read_text().count("Verified log bundle:") == 4, timeout=12)
            sim.stdin.write("quit" + chr(10))
            sim.stdin.flush()
            assert sim.wait(5) == 0, output.read_text()
            print("PASS manual requests still work and quit stops the cloud")
        finally:
            for proc in reversed(processes):
                stop(proc)
            for output in outputs:
                output.close()


if __name__ == "__main__":
    main()
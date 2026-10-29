"""Real-process MQTT/JSON -> C++ Agent -> multipart HTTP -> archive assertions."""
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import tarfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "mocks"))
from common import BusinessSimulator, business, cloud, wait_for
from business import make_registration
from cloud import CloudSimulator


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def read_bundle(path):
    with tarfile.open(path) as tar:
        return {m.name: tar.extractfile(m).read() for m in tar if m.isfile()}


def main():
    directory = ROOT / "run" / ("e2e-" + str(time.time_ns()))
    logs = directory / "logs"
    (logs / "app").mkdir(parents=True)
    (logs / "sdk/nested").mkdir(parents=True)
    # Selection uses filename dates; contents may be arbitrary or contain other dates.
    app_old = ("2026-10-14 23:59:59.999[INFO]: BEFORE_RANGE\n"
               "2026-10-15 00:00:00.000[INFO]: DAY_START\n"
               "2026-10-15 12:00:00.123[ERROR]: APP_ERROR\n"
               "  stack continuation\n"
               "2026-10-16 00:00:00.000[INFO]: AFTER_RANGE\n")
    app_new = "2026-10-16 10:00:00.000[INFO]: NEWEST_APP\n"
    sdk = ("2026-10-15 23:59:59.999[ERROR]: SDK_ERROR\n"
           "2026-10-16 00:00:00.000[INFO]: NEWEST_SDK\n")
    (logs / "app/2026_10_15_00_00_00.log").write_text(app_old)
    (logs / "app/2026_10_16_10_00_00.log").write_text(app_new)
    (logs / "app/ignore.txt").write_text("not a configured log\n")
    (logs / "sdk/nested/2026_10_15_23_59_59.log").write_text(sdk)
    (logs / "sdk/empty.log").touch()
    os.utime(logs / "app/2026_10_15_00_00_00.log", (100, 100))
    os.utime(logs / "app/2026_10_16_10_00_00.log", (200, 200))
    (logs / "app/escape.log").symlink_to("/etc/passwd")
    # No line-length limit and no text requirement: preserve all bytes.
    raw = b"x" * (1024 * 1024 + 1) + bytes([0, 255])
    (logs / "app/2026_10_15_00_00_01.log").write_bytes(raw)
    for name in ("no-date.log", "invalid-2026_02_30.log"):
        (logs / "app" / name).write_bytes(b"not selected by date")
        os.utime(logs / "app" / name, (1, 1))
    os.utime(logs / "app/2026_10_15_00_00_01.log", (2, 2))


    port = free_port()
    config = directory / "mosquitto.conf"
    config.write_text(f"listener {port} 127.0.0.1\nallow_anonymous true\npersistence false\n")
    handles = []
    processes = []
    sim = biz = None
    checks = []

    def check(name):
        checks.append(name)
        print("PASS", name, flush=True)

    def launch_agent():
        output = open(directory / f"agent-{len(processes)}.log", "w")
        handles.append(output)
        (directory / "device-agent.conf").write_text(chr(10).join([
            f"port={port}", "host=127.0.0.1", "device=demo-device", f"allowed-root={logs}", f"state-dir={directory / 'agent-state'}",
            f"work-dir={directory / 'work'}", "sources-config=", "allow-insecure-loopback-upload=true", ""]))
        proc = subprocess.Popen([str(ROOT / "build/device-agent")],
                                stdout=output, stderr=subprocess.STDOUT, cwd=directory)
        processes.append(proc)
        wait_for(lambda: "READY" in Path(output.name).read_text(), description="C++ Agent hello")
        return proc

    def stop(proc):
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(8)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

    try:
        broker_out = open(directory / "broker.log", "w")
        handles.append(broker_out)
        broker = subprocess.Popen(["mosquitto", "-c", str(config)], stdout=broker_out, stderr=subprocess.STDOUT)
        processes.append(broker)

        def broker_ready():
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=.1):
                    return True
            except OSError:
                return False
        wait_for(broker_ready, description="isolated MQTT broker")
        sim = CloudSimulator(port, "demo-device", directory / "cloud")
        registration = make_registration(logs)
        # Publish retained config and disconnect BEFORE Agent starts.
        biz = BusinessSimulator(port, "demo-device", registration)
        time.sleep(.2)
        biz.close()
        biz = None
        agent = launch_agent()
        wait_for(lambda: list((directory / "agent-state/registrations").glob("*.pb")), description="retained business registration")
        check("retained PB registration while business offline")

        latest = sim.make_command()
        sim.send(latest)
        result = sim.terminal(latest.command_id)
        assert result.status == cloud.CMD_SUCCEEDED, result
        files = read_bundle(sim.verified[latest.command_id])
        assert files == {"business/2026_10_16_10_00_00.log": app_new.encode(), "camera_sdk/nested/2026_10_15_23_59_59.log": sdk.encode()}, files
        assert not result.collect_logs.truncated
        check("latest: one complete file per source; recursive path; empty/pattern/symlink handling")
        upload = sim.requests[latest.command_id]
        assert re.fullmatch(r"log_demo-device_\d{8}_\d{6}\.tar\.gz", upload.filename), upload.filename
        check("bundle and upload filename follow log_<sn>_<yyyyMMdd_HHmmss>.tar.gz without <>")

        days = sim.make_command("days", start="2026-10-15")
        sim.send(days)
        result = sim.terminal(days.command_id)
        assert result.status == cloud.CMD_SUCCEEDED, result
        files = read_bundle(sim.verified[days.command_id])
        assert files == {
            "business/2026_10_15_00_00_00.log": app_old.encode(),
            "business/2026_10_15_00_00_01.log": raw,
            "camera_sdk/nested/2026_10_15_23_59_59.log": sdk.encode(),
        }, files.keys()
        assert result.collect_logs.time_from_ns == days.collect_logs.time_from_ns
        assert result.collect_logs.time_to_ns == days.collect_logs.time_to_ns
        check("filename date selection preserves whole files, binary bytes and lines over 1 MiB")

        history = len(sim.results[latest.command_id])
        upload_count = len(sim.uploads)
        sim.send(latest)
        wait_for(lambda: len(sim.results[latest.command_id]) > history)
        assert len(sim.uploads) == upload_count
        check("duplicate command returns previous result without another upload")

        for name, command, error in [
            ("empty date range", sim.make_command("days", start="2026-10-01"), "NO_LOGS_IN_RANGE"),
            ("unknown source", sim.make_command(sources=["missing"]), "UNKNOWN_SOURCE"),
        ]:
            sim.send(command)
            assert sim.terminal(command.command_id).error_code == error
            check(name)

        expired = sim.make_command()
        expired.expires_at_ns = 1
        sim.send(expired)
        assert sim.terminal(expired.command_id).error_code == "EXPIRED"
        check("expired command rejected")

        invalid = sim.make_command("days", start="2026-10-15")
        invalid.collect_logs.time_to_ns = invalid.collect_logs.time_from_ns
        sim.send(invalid)
        assert sim.terminal(invalid.command_id).error_code == "INVALID_ARGUMENT"
        check("invalid range rejected")

        sim.defer_grants = True
        running = sim.make_command()
        sim.send(running)
        wait_for(lambda: running.command_id in sim.requests)
        busy = sim.make_command()
        sim.send(busy)
        assert sim.terminal(busy.command_id).error_code == "BUSY"
        sim.defer_grants = False
        sim.send_grant(running.command_id)
        assert sim.terminal(running.command_id).status == cloud.CMD_SUCCEEDED
        check("one task at a time; BUSY rejection does not disturb running task")

        sim.deny_upload = True
        denied = sim.make_command()
        sim.send(denied)
        assert sim.terminal(denied.command_id).error_code == "UPLOAD_DENIED"
        sim.deny_upload = False
        check("upload credential denial reported")

        biz = BusinessSimulator(port, "demo-device", registration)
        wait_for(lambda: biz.acks)
        assert biz.acks[-1].accepted
        bad = business.Registration()
        bad.CopyFrom(registration)
        bad.config_version = 2
        bad.log_sources[0].path = "relative/path"
        assert not biz.register(bad).accepted
        check("business MQTT registration requires an absolute path")
        # Restore retained declaration; then completely remove business presence.
        assert biz.register(registration).accepted
        biz.close()
        biz = None

        # Clear retained declaration; recovery must use the Agent's persisted registry.
        sim.client.publish(f"local/v1/demo-device/biz/{registration.business_id}/up/register", b"", qos=1, retain=True).wait_for_publish()
        stop(agent)
        agent = launch_agent()
        history = len(sim.results[latest.command_id])
        original_file_id = next(r.collect_logs.file_id for r in sim.results[latest.command_id]
                                if r.status == cloud.CMD_SUCCEEDED)
        sim.send(latest)
        wait_for(lambda: len(sim.results[latest.command_id]) > history)
        assert sim.results[latest.command_id][-1].collect_logs.file_id == original_file_id
        recovered = sim.make_command()
        sim.send(recovered)
        assert sim.terminal(recovered.command_id).status == cloud.CMD_SUCCEEDED
        check("Agent restart: registry and command results recovered without live business or retained config")

        assert not sim.errors, sim.errors
        assert (logs / "app/2026_10_15_00_00_00.log").read_text() == app_old
        wait_for(lambda: not list((directory / "work").iterdir()), description="temporary bundle cleanup")
        check("cloud validates size/SHA256; source logs unchanged; temporary task files cleaned")
        report = {"status": "passed", "checks": checks, "artifacts": str(directory),
                  "latest_bundle": str(sim.verified[latest.command_id]),
                  "day_bundle": str(sim.verified[days.command_id])}
        (directory / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
        (ROOT / "run/latest-report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
        print(json.dumps(report, indent=2, ensure_ascii=False), flush=True)
    finally:
        if biz:
            biz.close()
        # Stop Agent before cloud/broker, to keep shutdown orderly.
        for proc in reversed(processes):
            if proc is not processes[0]:
                stop(proc)
        if sim:
            sim.close()
        for proc in processes:
            stop(proc)
        for handle in handles:
            handle.close()


if __name__ == "__main__":
    main()

"""Standalone business: writes sample log files, then registers their locations."""
import argparse
from datetime import datetime
from pathlib import Path
import time
from common import BusinessSimulator, business


def make_registration(log_root, business_id="camera_service", version=1):
    registration = business.Registration(business_id=business_id, config_version=version)
    for source_id, relative in [("business", "app"), ("camera_sdk", "sdk")]:
        source = registration.log_sources.add()
        source.source_id = source_id
        source.path = str((Path(log_root) / relative).resolve())
        source.recursive = True
        source.file_patterns.append("*.log")
    return registration


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=18883)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--device", default="demo-device")
    p.add_argument("--log-root", default="run/logs")
    args = p.parse_args()
    from zoneinfo import ZoneInfo
    stamp = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")
    for relative in ["app", "sdk"]:
        directory = Path(args.log_root) / relative
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / (stamp[:10] + ".log")).open("a", encoding="utf-8") as f:
            f.write(f"{stamp}[INFO]: {relative} simulator started\n")
            f.write(f"{stamp}[ERROR]: {relative} example diagnostic\n")
    sim = BusinessSimulator(args.port, args.device, make_registration(args.log_root), args.host)
    try:
        from common import wait_for
        ack = wait_for(lambda: sim.acks and sim.acks[-1], description="registration")
        print(ack, flush=True)
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        sim.close()


if __name__ == "__main__":
    main()

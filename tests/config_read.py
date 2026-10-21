"""Configuration-only entry point: malformed settings fail before connecting."""
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    cases = [
        (None, "cannot open config"),
        ("environment=production", "environment must be local or cloud"),
        ("environment=cloud", "cannot open config"),
        ("environment=local" + chr(10) + "port=1883", "cannot be mixed"),
        ("allowed-root=logs" + chr(10) + "allow-insecure-loopback-enroll=yes", "expected true or false"),
        ("config-file=missing.conf", "cannot open config"),
        ("config-file=device-agent.conf", "nesting too deep"),
        ("config-file=profile.conf" + chr(10) + "port=1883", "cannot be mixed"),
        ("config-file=", "must not be empty"),
        ("device=", "invalid device"),
        ("allowed-root=logs\nunknown-setting=true", "unknown config key"),
        ("allowed-root=logs\nport=0", "port must be"),
        ("allowed-root=logs\nport=18883junk", "port must be"),
        ("allowed-root=logs\nlocal-port=65536", "port must be"),
        ("allowed-root=logs\nallow-insecure-loopback-upload=yes", "expected true or false"),
        ("allowed-root=logs\nstate-dir=", "must not be empty"),
    ]
    for text, error in cases:
        with tempfile.TemporaryDirectory(prefix="agent-config-") as temp:
            directory = Path(temp)
            if text is not None:
                baseline = "host=127.0.0.1\nport=9\ndevice=config-test\nstate-dir=state\nwork-dir=work\n"
                if "environment=" in text or "config-file=" in text:
                    baseline = ""
                (directory / "device-agent.conf").write_text(baseline + text.replace(chr(92) + "n", chr(10)) + chr(10))
            result = subprocess.run([str(ROOT / "build/device-agent")],
                                    cwd=directory, capture_output=True, text=True, timeout=5)
            assert result.returncode == 1 and error in (result.stdout + result.stderr), result

    # Minimal cloud profile applies built-in paths. A corrupt local cache stops
    # before any real registration or MQTT traffic, so this check is offline.
    with tempfile.TemporaryDirectory(prefix="agent-minimal-profile-") as temp:
        directory = Path(temp)
        (directory / "device-agent.conf").write_text("environment=cloud" + chr(10))
        (directory / "device-agent.cloud.conf").write_text(
            "device=offline-profile-test\nhost=127.0.0.1\nport=9\nmodel=test-model\nstate-dir=run/cloud-agent\nwork-dir=run/cloud-work\nsources-config=log-sources.json\nenrollment-key=enr_synthetic\n")
        cache = directory / "run/cloud-agent/tokens/offline-profile-test.token"
        cache.parent.mkdir(parents=True)
        cache.touch()
        result = subprocess.run([str(ROOT / "build/device-agent")], cwd=directory,
                                capture_output=True, text=True, timeout=5)
        assert result.returncode == 1 and "invalid token file" in (result.stdout + result.stderr), result
        assert str(cache) in (result.stdout + result.stderr) and "environment=cloud" in (result.stdout + result.stderr)
        assert "enr_synthetic" not in (result.stdout + result.stderr)
        assert not (directory / "business-logs").exists()

        (directory / "device-agent.cloud.conf").write_text("enrollment-key=enr_synthetic" + chr(10))
        result = subprocess.run([str(ROOT / "build/device-agent")], cwd=directory,
                                capture_output=True, text=True, timeout=5)
        assert result.returncode == 1 and "invalid device" in (result.stdout + result.stderr)
    print("PASS explicit cloud profile loads settings without exposing credentials or making network calls")
    print("PASS config-only entry rejects missing/invalid settings and reports startup errors")


if __name__ == "__main__":
    main()
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
        ("host=localhost", "allowed-root is required"),
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
                (directory / "device-agent.conf").write_text(text.replace(chr(92) + "n", chr(10)) + chr(10))
            result = subprocess.run([str(ROOT / "build/device-agent")],
                                    cwd=directory, capture_output=True, text=True, timeout=5)
            assert result.returncode == 1 and error in result.stdout, result
            assert error in (directory / "run/agent.log").read_text()
    # Minimal cloud profile applies built-in paths. A corrupt local cache stops
    # before any real registration or MQTT traffic, so this check is offline.
    with tempfile.TemporaryDirectory(prefix="agent-minimal-profile-") as temp:
        directory = Path(temp)
        (directory / "device-agent.conf").write_text("environment=cloud" + chr(10))
        (directory / "device-agent.cloud.conf").write_text(
            "device=offline-profile-test" + chr(10) + "enrollment-key=enr_synthetic" + chr(10))
        cache = directory / "run/cloud-agent/tokens/offline-profile-test.token"
        cache.parent.mkdir(parents=True)
        cache.touch()
        result = subprocess.run([str(ROOT / "build/device-agent")], cwd=directory,
                                capture_output=True, text=True, timeout=5)
        assert result.returncode == 1 and "invalid token file" in result.stdout, result
        assert str(cache) in result.stdout and "environment=cloud" in result.stdout
        assert "enr_synthetic" not in result.stdout
        assert (directory / "business-logs").is_dir()

        (directory / "device-agent.cloud.conf").write_text("enrollment-key=enr_synthetic" + chr(10))
        result = subprocess.run([str(ROOT / "build/device-agent")], cwd=directory,
                                capture_output=True, text=True, timeout=5)
        assert result.returncode == 1 and "invalid device" in result.stdout
    print("PASS two-field cloud profile applies defaults without exposing credentials or making network calls")
    print("PASS config-only entry rejects missing/invalid settings and saves startup errors")


if __name__ == "__main__":
    main()
"""scripts/install_dashboard_web.sh: the dashboard on the web behind a password and HTTPS (Caddy). Run here with
stand-ins for caddy, systemctl, apt-get and the network; nothing is installed and nothing leaves this machine."""
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "install_dashboard_web.sh"
FAKE_HASH = "$2a$14$fakehashfakehashfakehashfakehashfakehashfakehashfakeh"

STUBS = {
    "systemctl": "exit 0",
    "apt-get": "exit 0",
    "chown": "exit 0",
    "ufw": 'echo "Status: inactive"',
    "curl": 'echo 167.71.59.188',
    "getent": 'echo "${GETENT_IP:-167.71.59.188} STREAM $3"',
    "sleep": "exit 0",
    "caddy": f"""case "$1" in
  hash-password) read -r pw; [ -n "$pw" ] && echo '{FAKE_HASH}';;
  validate) exit 0;;
esac""",
}


def run(tmp_path, stdin, **env):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name, body in STUBS.items():
        f = bin_dir / name
        f.write_text("#!/usr/bin/env bash\n" + body + "\n")
        f.chmod(0o755)
    caddyfile = tmp_path / "Caddyfile"
    e = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "CADDYFILE": str(caddyfile), **env}
    r = subprocess.run(["bash", str(SCRIPT)], input=stdin, capture_output=True, text=True, env=e, timeout=30)
    return r, caddyfile


needs_root = pytest.mark.skipif(os.geteuid() != 0, reason="the script runs as root only")


@needs_root
def test_writes_a_password_protected_proxy_to_the_local_dashboard_only(tmp_path):
    r, cf = run(tmp_path, "sultanbot.duckdns.org\na-long-password-123\na-long-password-123\n")
    assert r.returncode == 0, r.stderr
    text = cf.read_text()
    assert "sultanbot.duckdns.org {" in text and f"sultan {FAKE_HASH}" in text
    assert text.index("basicauth") < text.index("reverse_proxy")
    assert [ln.strip() for ln in text.splitlines() if "reverse_proxy" in ln] == ["reverse_proxy 127.0.0.1:8050"]
    assert oct(cf.stat().st_mode & 0o777) == "0o640"
    out = r.stdout + r.stderr
    assert "a-long-password-123" not in out and FAKE_HASH not in out           # never printed
    assert "https://sultanbot.duckdns.org" in r.stdout


@needs_root
def test_refuses_a_short_or_mismatched_password_until_a_good_one(tmp_path):
    r, cf = run(tmp_path, "sultanbot.duckdns.org\nshort\nshort\nlong-enough-one-1\nother-one-12345\n"
                          "long-enough-one-1\nlong-enough-one-1\n")
    assert r.returncode == 0 and "Too short" in r.stdout and "do not match" in r.stdout


@needs_root
def test_refuses_a_name_that_points_elsewhere_and_changes_nothing(tmp_path):
    r, cf = run(tmp_path, "sultanbot.duckdns.org\n", GETENT_IP="1.2.3.4")
    assert r.returncode == 1 and "points at 1.2.3.4" in r.stderr and not cf.exists()


@needs_root
def test_refuses_something_that_is_not_a_host_name(tmp_path):
    r, cf = run(tmp_path, "http://x y\n")
    assert r.returncode == 1 and "not a host name" in r.stderr and not cf.exists()


def test_the_password_never_goes_on_a_command_line_and_the_dashboard_stays_local():
    s = SCRIPT.read_text()
    assert "--plaintext" not in s and "read -r -s -p" in s
    assert "reverse_proxy 127.0.0.1:8050" in s and "0.0.0.0" not in s
    dash = (ROOT / "src" / "bot" / "dashboard.py").read_text()
    assert 'LOOPBACK = ("127.0.0.1", "localhost", "::1")' in dash         # the dashboard itself is unchanged

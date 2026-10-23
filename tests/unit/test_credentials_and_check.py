import argparse
from pathlib import Path

import pytest

from bot.cli import _check_binance
from bot.exchange.credentials import CredentialsError, load_credentials
from tests.unit.test_binance_readonly import SAFE_PERMS, make

ROOT = Path(__file__).resolve().parents[2]
KEY = "Ab12" * 16


def setup(tmp_path, env_mode=0o600, key_mode=0o600, key_text="-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----\n"):
    pk = tmp_path / "k.pem"
    pk.write_text(key_text)
    pk.chmod(key_mode)
    env = tmp_path / ".env"
    env.write_text(f'# comment\nBINANCE_API_KEY="{KEY}"\nBINANCE_PRIVATE_KEY_PATH={pk}\n')
    env.chmod(env_mode)
    return env, pk


def test_loads_and_hides_key(tmp_path):
    env, pk = setup(tmp_path)
    c = load_credentials(env_file=env, environ={})
    assert c.api_key == KEY and c.private_key_path == pk
    assert KEY not in repr(c) and KEY not in str(c)


def test_environment_overrides_file(tmp_path):
    env, _ = setup(tmp_path)
    c = load_credentials(env_file=env, environ={"BINANCE_API_KEY": "Z" * 64})
    assert c.api_key == "Z" * 64


@pytest.mark.parametrize("kw,match", [
    ({"env_mode": 0o644}, "settings file"),
    ({"key_mode": 0o640}, "private key file"),
    ({"key_text": "not a key"}, "not a PEM private key"),
])
def test_refuses_unsafe_or_bad_files(tmp_path, kw, match):
    env, _ = setup(tmp_path, **kw)
    with pytest.raises(CredentialsError, match=match):
        load_credentials(env_file=env, environ={})


def test_missing_key_message(tmp_path):
    with pytest.raises(CredentialsError, match="make_binance_key.sh"):
        load_credentials(env_file=tmp_path / "none.env", environ={})


# ---------------------------------------------------------------- bot check-binance
def args(**kw):
    return argparse.Namespace(config=str(ROOT / "config" / "default.yaml"), public_only=False, details=False, **kw)


def run_check(capsys, perms=None, creds_error=None):
    ex = make()
    if perms is not None:
        ex.signed_client.routes = {**ex.signed_client.routes, "/sapi/v1/account/apiRestrictions": perms}
    server_ms = ex.server_time_ms()
    ex.clock = lambda: server_ms / 1000        # local clock equal to Binance's: zero skew

    def loader():
        if creds_error:
            raise CredentialsError(creds_error)
        return ex.credentials

    code = _check_binance(args(), make_exchange=lambda **kw: ex, load_creds=loader)
    return code, capsys.readouterr().out


def test_check_passes_with_safe_key(capsys):
    code, out = run_check(capsys)
    assert code == 0 and "SAFE: reading only" in out and "ALL CHECKS PASSED" in out
    assert "LDBTC" in out and "taker fee 0.100%" in out


def test_check_stops_on_trading_key(capsys):
    code, out = run_check(capsys, perms={**SAFE_PERMS, "enableSpotAndMarginTrading": True})
    assert code == 3 and "STOP" in out and "enableSpotAndMarginTrading is ON" in out
    assert "Balances" not in out                       # nothing else is read with an unsafe key


def test_check_reports_missing_key(capsys):
    code, out = run_check(capsys, creds_error="BINANCE_API_KEY is not set")
    assert code == 2 and "NOT READY" in out


@pytest.mark.parametrize("args", [[], ["--testnett"], ["--live"], ["--testnet", "--live-read-only"]])
def test_the_key_script_refuses_a_missing_or_unknown_option(tmp_path, args):
    """A typo must never overwrite the wrong key: the script stops before touching anything."""
    import subprocess
    from pathlib import Path
    script = Path(__file__).resolve().parents[2] / "scripts" / "make_binance_key.sh"
    r = subprocess.run(["bash", str(script), *args], capture_output=True, text=True, timeout=30,
                       env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}, input="")
    assert r.returncode == 1 and "nothing was changed" in (r.stderr + r.stdout).lower(), r.stderr
    assert not (tmp_path / ".secrets").exists()

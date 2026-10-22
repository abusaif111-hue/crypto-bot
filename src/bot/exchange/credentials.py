"""Loads Binance credentials from the environment or a .env file.

Secrets are never printed: the object's repr hides them, and errors name the
setting that is wrong, never its value. The private key stays in its file; only
its path is passed on to Binance's official SDK, which reads it to sign requests.
"""
from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]


class CredentialsError(RuntimeError):
    pass


@dataclass(frozen=True)
class Credentials:
    api_key: str = field(repr=False)
    private_key_path: Path

    def __repr__(self) -> str:  # never show the key, even in tracebacks
        return f"Credentials(api_key='{self.api_key[:4]}…', private_key_path='{self.private_key_path}')"


def read_env_file(path: Path) -> dict[str, str]:
    """Minimal KEY=VALUE parser: comments, blank lines and optional quotes."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def _check_private(path: Path, what: str) -> None:
    mode = path.stat().st_mode
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise CredentialsError(f"{what} {path} can be read by other users; run: chmod 600 {path}")


def load_credentials(env_file: Path | None = None, environ: dict[str, str] | None = None) -> Credentials:
    """Environment variables win over the .env file."""
    environ = dict(os.environ if environ is None else environ)
    env_path = Path(environ.get("BOT_ENV_FILE", env_file or REPO_ROOT / ".env"))
    file_values = read_env_file(env_path)
    if env_path.is_file():
        _check_private(env_path, "The settings file")

    def get(name: str) -> str:
        return (environ.get(name) or file_values.get(name) or "").strip()

    api_key = get("BINANCE_API_KEY")
    key_path = get("BINANCE_PRIVATE_KEY_PATH")
    if not api_key:
        raise CredentialsError(
            f"BINANCE_API_KEY is not set (looked in the environment and {env_path}). "
            "Run scripts/make_binance_key.sh to store it.")
    if not api_key.isalnum() or not 16 <= len(api_key) <= 128:
        raise CredentialsError("BINANCE_API_KEY does not look like a Binance API key (letters and digits only)")
    if not key_path:
        raise CredentialsError("BINANCE_PRIVATE_KEY_PATH is not set")
    pk = Path(key_path).expanduser()
    if not pk.is_file():
        raise CredentialsError(f"private key file not found: {pk}")
    _check_private(pk, "The private key file")
    head = pk.read_text(errors="replace")[:64]
    if "PRIVATE KEY" not in head:
        raise CredentialsError(f"{pk} is not a PEM private key file")
    return Credentials(api_key=api_key, private_key_path=pk)

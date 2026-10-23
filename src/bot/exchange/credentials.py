"""Loads Binance credentials from the environment or a .env file.

Secrets are never printed: the object's repr hides them, and errors name the
setting that is wrong, never its value. The private key stays in its file; only
its path is passed on to Binance's official SDK, which reads it to sign requests.
"""
from __future__ import annotations

import os
import re
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


def load_credentials(env_file: Path | None = None, environ: dict[str, str] | None = None,
                     prefix: str = "BINANCE") -> Credentials:
    """Environment variables win over the .env file. `prefix` picks the key: BINANCE (the live read-only
    key) or BINANCE_TESTNET (the testnet trading key), so the two can never be mixed up."""
    environ = dict(os.environ if environ is None else environ)
    env_path = Path(environ.get("BOT_ENV_FILE", env_file or REPO_ROOT / ".env"))
    file_values = read_env_file(env_path)
    if env_path.is_file():
        _check_private(env_path, "The settings file")

    def get(name: str) -> str:
        return (environ.get(name) or file_values.get(name) or "").strip()

    key_name, path_name = f"{prefix}_API_KEY", f"{prefix}_PRIVATE_KEY_PATH"
    api_key = get(key_name)
    key_path = get(path_name)
    if not api_key:
        raise CredentialsError(
            f"{key_name} is not set (looked in the environment and {env_path}). "
            f"Run scripts/make_binance_key.sh {'--testnet' if prefix == 'BINANCE_TESTNET' else '--live-read-only'} "
            f"to store it.")
    if not api_key.isalnum() or not 16 <= len(api_key) <= 128:
        raise CredentialsError(f"{key_name} does not look like a Binance API key (letters and digits only)")
    if not key_path:
        raise CredentialsError(f"{path_name} is not set")
    pk = Path(key_path).expanduser()
    if not pk.is_file():
        raise CredentialsError(f"private key file not found: {pk}")
    _check_private(pk, "The private key file")
    head = pk.read_text(errors="replace")[:64]
    if "PRIVATE KEY" not in head:
        raise CredentialsError(f"{pk} is not a PEM private key file")
    return Credentials(api_key=api_key, private_key_path=pk)


# ---------------------------------------------------------------- Telegram alerts (module 5e)
_TELEGRAM_TOKEN = re.compile(r"^\d{5,16}:[A-Za-z0-9_-]{30,64}$")


@dataclass(frozen=True)
class TelegramSettings:
    token: str = field(repr=False)
    chat_id: str

    def __repr__(self) -> str:
        return f"TelegramSettings(chat_id={self.chat_id!r})"


def is_telegram_token(token: str) -> bool:
    return bool(_TELEGRAM_TOKEN.match(token.strip()))


def env_path(environ: dict[str, str] | None = None) -> Path:
    environ = dict(os.environ if environ is None else environ)
    return Path(environ.get("BOT_ENV_FILE", REPO_ROOT / ".env"))


def load_telegram(env_file: Path | None = None, environ: dict[str, str] | None = None) -> TelegramSettings:
    """TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID from the environment or .env (bot alerts setup writes them)."""
    environ = dict(os.environ if environ is None else environ)
    path = Path(environ.get("BOT_ENV_FILE", env_file or REPO_ROOT / ".env"))
    values = read_env_file(path)
    if path.is_file():
        _check_private(path, "The settings file")
    token = (environ.get("TELEGRAM_BOT_TOKEN") or values.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat = (environ.get("TELEGRAM_CHAT_ID") or values.get("TELEGRAM_CHAT_ID") or "").strip()
    if not token:
        raise CredentialsError(f"TELEGRAM_BOT_TOKEN is not set (looked in the environment and {path}); "
                               f"run: bot alerts setup")
    if not is_telegram_token(token):
        raise CredentialsError("TELEGRAM_BOT_TOKEN does not look like a token from @BotFather")
    if not re.fullmatch(r"-?\d{1,20}", chat):
        raise CredentialsError("TELEGRAM_CHAT_ID is not set or not a number; run: bot alerts setup")
    return TelegramSettings(token, chat)


def set_env_values(path: Path, values: dict[str, str]) -> None:
    """Set KEY=VALUE lines in the settings file (others kept), written privately (mode 600)."""
    path = Path(path)
    lines = path.read_text().splitlines() if path.is_file() else []
    left = dict(values)
    out = []
    for raw in lines:
        key = raw.split("=", 1)[0].strip()
        if "=" in raw and not raw.lstrip().startswith("#") and key in left:
            out.append(f"{key}={left.pop(key)}")
        else:
            out.append(raw)
    out += [f"{k}={v}" for k, v in left.items()]
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(out) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)

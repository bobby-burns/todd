"""Encrypted secret storage. Secrets are never placed in model context; tools read them directly."""

from __future__ import annotations

import os

from cryptography.fernet import Fernet, InvalidToken

from .config import config
from .db import Secret, select, session, utcnow


def _load_key() -> bytes:
    if config.secret_key:
        return config.secret_key.encode()
    key_file = config.data_dir / "secret.key"
    if key_file.exists():
        return key_file.read_bytes().strip()
    key = Fernet.generate_key()
    key_file.write_bytes(key)
    os.chmod(key_file, 0o600)
    return key


_fernet = Fernet(_load_key())


def set_secret(name: str, value: str) -> None:
    token = _fernet.encrypt(value.encode()).decode()
    with session() as s:
        row = s.get(Secret, name) or Secret(name=name, ciphertext=token)
        row.ciphertext = token
        row.updated_at = utcnow()
        s.add(row)
        s.commit()


def get_secret(name: str, default: str | None = None) -> str | None:
    """Vault first, then environment variable of the same name."""
    with session() as s:
        row = s.get(Secret, name)
    if row:
        try:
            return _fernet.decrypt(row.ciphertext.encode()).decode()
        except InvalidToken:
            return default
    return os.getenv(name) or default


def delete_secret(name: str) -> bool:
    with session() as s:
        row = s.get(Secret, name)
        if not row:
            return False
        s.delete(row)
        s.commit()
        return True


def list_secrets() -> list[dict]:
    with session() as s:
        rows = s.exec(select(Secret).order_by(Secret.name)).all()
    out = []
    for r in rows:
        value = get_secret(r.name) or ""
        masked = (value[:3] + "…" + value[-4:]) if len(value) > 10 else "••••"
        out.append({"name": r.name, "masked": masked, "updated_at": r.updated_at})
    return out


def has_secret(name: str) -> bool:
    return bool(get_secret(name))


PROTECTED_EXACT = {"VERCEL_TOKEN", "GITHUB_TOKEN", "OPENAI_COMPATIBLE_API_KEY", "TODD_API_TOKEN"}


def is_protected(name: str) -> bool:
    """Integration tokens, model-provider keys, card fields and CLI sign-ins (CLI_STATE_*): tools use them
    internally, but agents may never inject them into values ({{secret:NAME}}) or overwrite them."""
    from .llm import PROVIDER_KEYS

    n = name.upper()
    return n in PROTECTED_EXACT or n in PROVIDER_KEYS.values() or n.startswith(("CARD_", "CLI_STATE_"))


def secret_values(min_len: int = 6) -> list[str]:
    """All vault values (longest first) — used to scrub tool output before it reaches the model or the UI."""
    with session() as s:
        names = [r.name for r in s.exec(select(Secret)).all()]
    vals = [v for v in (get_secret(n) for n in names) if v and len(v) >= min_len]
    for env_name in ("VERCEL_TOKEN", "GITHUB_TOKEN"):
        v = os.getenv(env_name)
        if v and len(v) >= min_len:
            vals.append(v)
    return sorted(set(vals), key=len, reverse=True)


def scrub(text: str) -> str:
    for v in secret_values():
        if v in text:
            text = text.replace(v, "***")
    return text

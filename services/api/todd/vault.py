"""Encrypted secret storage. Secrets are never placed in model context; tools read them directly."""

from __future__ import annotations

import os

from cryptography.fernet import Fernet, InvalidToken

from .config import config
from .db import Run, Secret, SecretOrigin, select, session, utcnow


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


def set_secret(name: str, value: str, *, source: str | None = None, track: bool = True) -> None:
    """Store a secret. It's credited to the run (and agent) saving it, or to `source` outside a run ("you" by
    default); `track=False` keeps the current credit (e.g. a CLI refreshing its own sign-in)."""
    token = _fernet.encrypt(value.encode()).decode()
    with session() as s:
        row = s.get(Secret, name) or Secret(name=name, ciphertext=token)
        row.ciphertext = token
        row.updated_at = utcnow()
        s.add(row)
        origin = s.get(SecretOrigin, name)
        if track or origin is None:
            origin = origin or SecretOrigin(name=name)
            origin.run_id, origin.agent, origin.source = _saver(source)
            origin.updated_at = utcnow()
            s.add(origin)
        s.commit()


def _saver(source: str | None) -> tuple[str | None, str | None, str]:
    from .runtime import current_agent_id
    from .sdk import _current_ctx

    ctx = _current_ctx.get()
    if ctx is not None:
        return ctx.run_id, current_agent_id(), "run"
    return None, None, source or "you"


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
        origin = s.get(SecretOrigin, name)
        if origin:
            s.delete(origin)
        if not row:
            s.commit()
            return False
        s.delete(row)
        s.commit()
        return True


def list_secrets(run_id: str | None = None) -> list[dict]:
    """Names and masked values (never the values), each with where it came from: {run_id, run_title, agent,
    source}. `run_id` limits it to the keys that run saved."""
    with session() as s:
        rows = s.exec(select(Secret).order_by(Secret.name)).all()
        origins = {o.name: o for o in s.exec(select(SecretOrigin)).all()}
        run_ids = {o.run_id for o in origins.values() if o.run_id}
        titles = {r.id: r.title for r in s.exec(select(Run).where(Run.id.in_(run_ids))).all()} if run_ids else {}
    out = []
    for r in rows:
        o = origins.get(r.name)
        if run_id and (not o or o.run_id != run_id):
            continue
        value = get_secret(r.name) or ""
        masked = (value[:3] + "…" + value[-4:]) if len(value) > 10 else "••••"
        rid = o.run_id if o else None
        out.append({"name": r.name, "masked": masked, "updated_at": r.updated_at, "run_id": rid,
                    "run_title": titles.get(rid) if rid else None, "agent": o.agent if o else None,
                    "source": o.source if o else None})  # None: saved before Todd kept track
    return out


def has_secret(name: str) -> bool:
    return bool(get_secret(name))


PROTECTED_EXACT = {"VERCEL_TOKEN", "GITHUB_TOKEN", "EXPO_TOKEN", "OPENAI_COMPATIBLE_API_KEY", "TODD_API_TOKEN"}


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

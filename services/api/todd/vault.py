"""Encrypted secret storage. Secrets are never placed in model context; tools read them directly."""

from __future__ import annotations

import os
import re

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


def set_secret(name: str, value: str, *, source: str | None = None, track: bool = True,
               origin: tuple[str, str] | None = None) -> None:
    """Store a secret. It's credited to the run (and agent) saving it, or to `source` outside a run ("you" by
    default), or to `origin` (run_id, agent) when given; `track=False` keeps the current credit (e.g. a CLI refreshing
    its own sign-in)."""
    token = _fernet.encrypt(value.encode()).decode()
    with session() as s:
        row = s.get(Secret, name) or Secret(name=name, ciphertext=token)
        row.ciphertext = token
        row.updated_at = utcnow()
        s.add(row)
        credit = s.get(SecretOrigin, name)
        if track or credit is None:
            credit = credit or SecretOrigin(name=name)
            credit.run_id, credit.agent, credit.source = (origin[0], origin[1], "run") if origin else _saver(source)
            credit.updated_at = utcnow()
            s.add(credit)
        s.commit()
    _changed()


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
        _changed()
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
        masked = ("••••" + value[-4:]) if len(value) >= 20 and "\n" not in value else "••••"
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


_values_cache: tuple[float, int, list[str]] | None = None
_version = 0  # bumped on every write, so a new secret is scrubbed right away


def _changed() -> None:
    global _version
    _version += 1


def secret_values(min_len: int = 6) -> list[str]:
    """All vault values (longest first), plus the tokens inside CLI sign-in snapshots, the env fallbacks, and the
    forms a value takes when it's escaped or encoded (JSON, URL, base64). Used to scrub text before it reaches the
    model or the UI. Cached until the vault changes (or for a few seconds)."""
    import time

    global _values_cache
    now = time.monotonic()
    if _values_cache and _values_cache[1] == _version and now - _values_cache[0] < 10:
        return _values_cache[2]
    with session() as s:
        names = [r.name for r in s.exec(select(Secret)).all()]
    raw: list[str] = []
    for n in names:
        v = get_secret(n)
        if not v:
            continue
        raw.append(v)
        if n.upper().startswith("CLI_STATE_"):
            raw.extend(_snapshot_tokens(v))
    for env_name in ("VERCEL_TOKEN", "GITHUB_TOKEN"):
        v = os.getenv(env_name)
        if v:
            raw.append(v)
    vals: set[str] = set()
    for v in raw:
        if len(v) < min_len:
            continue
        vals.add(v)
        vals.update(_forms(v))
    out = sorted((v for v in vals if len(v) >= min_len), key=len, reverse=True)
    _values_cache = (now, _version, out)
    return out


def _forms(v: str) -> list[str]:
    """How a secret looks once it's JSON-escaped, URL-encoded or base64-encoded (e.g. `echo $KEY | base64`)."""
    import base64
    import json as _json
    from urllib.parse import quote

    forms = [_json.dumps(v)[1:-1], quote(v, safe="")]
    for raw in (v, v + "\n"):
        forms.append(base64.b64encode(raw.encode()).decode().rstrip("="))
    if "\n" in v:  # multi-line (keys): each long line on its own, as they'd show in a partial print
        forms.extend(line.strip() for line in v.splitlines() if len(line.strip()) >= 24)
    return [f for f in forms if f != v]


def _snapshot_tokens(blob: str) -> list[str]:
    """Token-like strings inside a CLI sign-in snapshot (a base64 tar of the CLI's config), so a token read from the
    unpacked files mid-command is still scrubbed."""
    import base64
    import io
    import tarfile

    out: list[str] = []
    try:
        data = base64.b64decode(blob)
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            for m in tar.getmembers()[:200]:
                if not m.isfile() or m.size > 256 * 1024:
                    continue
                f = tar.extractfile(m)
                text = (f.read() if f else b"").decode(errors="ignore")
                out.extend(re.findall(r"[A-Za-z0-9_\-.]{24,}", text))
    except Exception:  # noqa: BLE001  (not a snapshot we can read: its own value is still scrubbed)
        return []
    return [t for t in set(out) if any(c.isdigit() for c in t) and any(c.isalpha() for c in t)][:500]


def scrub(text: str, patterns: bool = True) -> str:
    """Hide vault values (and their encoded forms) and, with `patterns`, anything that looks like a secret by format
    (see redact.known)."""
    for v in secret_values():
        if v in text:
            text = text.replace(v, "***")
    if patterns:
        from . import redact

        text = redact.known(text)
    return text

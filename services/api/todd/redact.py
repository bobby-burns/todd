"""Keep secrets out of what models see, and out of the timeline.

`vault.scrub` removes values Todd already knows (the vault). This module catches secrets it doesn't know yet, by
their shape:

* `known(text)`: formats that are always secrets (private key blocks, Stripe/GitHub/Slack/OpenAI/Anthropic/npm/…
  tokens, AWS access key ids), credentials in URLs (`https://user:pass@…`) and secret-bearing URL parameters
  (`?access_token=`, `#id_token=`, `code=`, signatures). Safe on any tool output.
* `page(text)`: `known` plus what's only a secret when a web page shows it: JWTs (e.g. a Supabase service key),
  Google API keys and long random-looking strings. For page text, DOM snapshots and anything read off a page (not for
  command output, where long ids like commit hashes are normal).
* `deep(obj, fn)`: apply to every string in a tool result before it's serialized (so escaping can't hide a match).

The browser uses the same page patterns to blur what's on screen before a screenshot (see tools/page_guard.py).
"""

from __future__ import annotations

import re
from typing import Any, Callable

MARK = "[secret hidden]"
PAGE_MARK = "[secret hidden: save it with browser_save_secret instead of reading it]"

# (name, regex source). Sources stay compatible with JavaScript so the page guard can reuse them.
KNOWN_SOURCES: list[tuple[str, str]] = [
    ("private key", r"-----BEGIN[A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?(?:-----END[A-Z0-9 ]*PRIVATE KEY-----|$)"),
    ("stripe", r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}"),
    ("stripe webhook", r"\bwhsec_[A-Za-z0-9]{20,}"),
    ("github", r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}|\bgithub_pat_[A-Za-z0-9_]{40,}"),
    ("gitlab", r"\bglpat-[A-Za-z0-9_-]{20,}"),
    ("slack", r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    ("anthropic", r"\bsk-ant-[A-Za-z0-9_-]{20,}"),
    ("openai", r"\bsk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_-]{32,}"),
    ("aws", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    ("npm", r"\bnpm_[A-Za-z0-9]{36}\b"),
    ("sendgrid", r"\bSG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}"),
    ("hugging face", r"\bhf_[A-Za-z0-9]{30,}"),
    ("replicate", r"\br8_[A-Za-z0-9]{30,}"),
    ("resend", r"\bre_[A-Za-z0-9]{8,}_[A-Za-z0-9]{16,}"),
    ("vercel", r"\b(?:vercel|vc)_[A-Za-z0-9]{24,}"),
]
PAGE_SOURCES: list[tuple[str, str]] = [
    ("jwt", r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    ("google api key", r"\bAIza[0-9A-Za-z_-]{35}\b"),
    # 32+ characters mixing upper case, lower case and digits (random keys; not words, hashes or UUIDs)
    ("random", r"(?<![A-Za-z0-9_/+=.-])(?=[A-Za-z0-9_-]*[A-Z])(?=[A-Za-z0-9_-]*[a-z])(?=[A-Za-z0-9_-]*[0-9])"
               r"[A-Za-z0-9_-]{32,}(?![A-Za-z0-9_-])"),
]
URL_PARAM = re.compile(
    r"([?&#;](?:access_token|refresh_token|id_token|token|auth|authorization|api[_-]?key|apikey|key|secret|"
    r"client_secret|password|passwd|pwd|code|sig|signature|x-amz-signature|x-amz-credential|x-amz-security-token|"
    r"x-goog-signature|x-goog-credential|session|session_id|sessionid|jwt|otp)=)([^&#\s\"'<>]+)", re.IGNORECASE)
URL_USERINFO = re.compile(r"(\b[a-z][a-z0-9+.-]*://[^/\s:@\"'<>]+:)([^@\s/\"'<>]+)(@)", re.IGNORECASE)
_KEEP = re.compile(r"\{\{secret:[A-Z0-9_]+\}\}|<secret>[a-z_]+</secret>")  # placeholders are fine

_KNOWN = re.compile("|".join(f"(?:{src})" for _, src in KNOWN_SOURCES))
_PAGE = re.compile("|".join(f"(?:{src})" for _, src in PAGE_SOURCES))


def _sub(pattern: re.Pattern, text: str, mark: str) -> str:
    return pattern.sub(lambda m: m.group(0) if _KEEP.fullmatch(m.group(0)) else mark, text)


_LOOSE = ("key=", "code=", "auth=", "session=")  # common names: only long values are treated as secrets


def _param(m: re.Match) -> str:
    name, value = m.group(1).lower(), m.group(2)
    long_enough = len(value) >= (20 if name[1:] in _LOOSE else 6)
    return m.group(1) + "***" if long_enough else m.group(0)


def hidden_markers(text: str) -> bool:
    """Whether text contains something Todd hid (so writing it back would destroy the real value)."""
    return MARK in text or "[secret hidden" in text or "••••••••" in text


def urls(text: str) -> str:
    """Hide passwords in URLs and the values of secret-bearing URL parameters."""
    if "=" in text:
        text = URL_PARAM.sub(_param, text)
    if "@" in text:
        text = URL_USERINFO.sub(r"\1***\3", text)
    return text


def known(text: str, mark: str = MARK) -> str:
    """Secrets recognizable by format, anywhere (tool output, messages, events)."""
    if not text:
        return text
    return urls(_sub(_KNOWN, text, mark))


def page(text: str) -> str:
    """For what a web page shows: known formats plus JWTs, Google API keys and long random-looking strings."""
    if not text:
        return text
    return _sub(_PAGE, known(text, PAGE_MARK), PAGE_MARK)


def has_page_secret(text: str) -> bool:
    return bool(text) and (bool(_KNOWN.search(text)) or bool(_PAGE.search(text)))


def deep(obj: Any, fn: Callable[[str], str]) -> Any:
    """Apply `fn` to every string inside a tool result (dicts, lists, tuples), keys included."""
    if isinstance(obj, str):
        return fn(obj)
    if isinstance(obj, dict):
        return {deep(k, fn) if isinstance(k, str) else k: deep(v, fn) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return type(obj)(deep(v, fn) for v in obj)
    return obj


def js_sources() -> list[str]:
    """The page patterns as JavaScript regex sources (for blurring them on screen)."""
    return [src for _, src in KNOWN_SOURCES + PAGE_SOURCES]

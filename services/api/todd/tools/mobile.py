"""The `mobile` toolset: build and ship iOS and Android apps without a Mac.

* `eas`: Expo's EAS CLI in the sandbox, signed in with the human's EXPO_TOKEN. Builds run on Expo's servers (macOS
  workers for iOS), so the sandbox needs neither Xcode nor the Android SDK. The App Store Connect key and the Google
  Play service account go to the CLI as files in a private temp dir that exists only for that command.
* `app_store_connect` / `google_play`: the stores' REST APIs, called from the API process with a token signed per
  call. Keys never reach the sandbox or the model.

Submitting to a store, publishing an over-the-air update and anything that goes out to reviewers or testers need the
human's approval. That's enforced here, not left to the model.
"""

from __future__ import annotations

import base64
import json
import re
import shlex
import textwrap
import time
from typing import Any

import httpx

from .. import vault
from ..sdk import ToolError, get_agent_id, get_ctx, todd_tool
from . import sandbox

ASC_API = "https://api.appstoreconnect.apple.com"
PLAY_API = "https://androidpublisher.googleapis.com/androidpublisher/v3/"
PLAY_SCOPE = "https://www.googleapis.com/auth/androidpublisher"

# Vault names. Key id, issuer id and team id aren't secret; the token, private key and service account are protected
# (vault.PROTECTED_EXACT): only these tools use them.
EXPO_TOKEN = "EXPO_TOKEN"
ASC_KEY_ID, ASC_ISSUER_ID, ASC_PRIVATE_KEY = "ASC_KEY_ID", "ASC_ISSUER_ID", "ASC_PRIVATE_KEY"
APPLE_TEAM_ID = "APPLE_TEAM_ID"
PLAY_SERVICE_ACCOUNT = "GOOGLE_PLAY_SERVICE_ACCOUNT_JSON"

NO_EXPO = ("EXPO_TOKEN isn't set. Ask the human to create an access token at expo.dev (Account settings → Access "
           "tokens) and add it in Settings → Integrations → Mobile apps.")


# ------------------------------------------------------------------------------------------ keys
def pem(value: str, kind: str = "PRIVATE KEY") -> str:
    """Normalize a PEM key pasted into a one-line field (newlines stripped) back into a valid PEM."""
    body = re.sub(r"-----(BEGIN|END) [A-Z ]+-----", "", value)
    body = re.sub(r"\s+|\\n", "", body)
    if not body:
        raise ToolError("the private key is empty")
    return f"-----BEGIN {kind}-----\n" + "\n".join(textwrap.wrap(body, 64)) + f"\n-----END {kind}-----\n"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _jwt(header: dict, claims: dict, key_pem: str, alg: str) -> str:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec, padding
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

    signing_input = (_b64url(json.dumps(header, separators=(",", ":")).encode()) + "."
                     + _b64url(json.dumps(claims, separators=(",", ":")).encode())).encode()
    try:
        key = serialization.load_pem_private_key(key_pem.encode(), password=None)
    except (ValueError, TypeError) as e:
        raise ToolError(f"the private key in the vault can't be read ({type(e).__name__})") from None
    if alg == "ES256":
        if not isinstance(key, ec.EllipticCurvePrivateKey):
            raise ToolError("the App Store Connect key must be the .p8 (EC) key Apple gave you")
        r, s = decode_dss_signature(key.sign(signing_input, ec.ECDSA(hashes.SHA256())))
        sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")  # JWS wants raw r||s, not DER
    else:
        sig = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())  # type: ignore[union-attr, call-arg]
    return signing_input.decode() + "." + _b64url(sig)


def asc_token(now: float | None = None) -> str:
    """A short-lived App Store Connect API token (ES256, 20 minutes at most)."""
    kid, iss, key = (vault.get_secret(n) for n in (ASC_KEY_ID, ASC_ISSUER_ID, ASC_PRIVATE_KEY))
    missing = [n for n, v in ((ASC_KEY_ID, kid), (ASC_ISSUER_ID, iss), (ASC_PRIVATE_KEY, key)) if not v]
    if missing:
        raise ToolError(f"App Store Connect API key not set up ({', '.join(missing)} missing). Ask the human to create "
                        "a Team key in App Store Connect → Users and Access → Integrations and add it in Settings → "
                        "Integrations → Mobile apps.")
    now = int(now or time.time())
    return _jwt({"alg": "ES256", "kid": kid, "typ": "JWT"},
                {"iss": iss, "iat": now, "exp": now + 1140, "aud": "appstoreconnect-v1"}, pem(key or ""), "ES256")


def play_account() -> dict[str, Any]:
    raw = vault.get_secret(PLAY_SERVICE_ACCOUNT)
    if not raw:
        raise ToolError(f"{PLAY_SERVICE_ACCOUNT} isn't set. Ask the human to create a service account with access to "
                        "their Play Console (Users and permissions) and add its JSON key in Settings → Integrations → "
                        "Mobile apps.")
    try:
        acct = json.loads(raw)
        assert acct["client_email"] and acct["private_key"]
    except Exception:  # noqa: BLE001
        raise ToolError(f"{PLAY_SERVICE_ACCOUNT} isn't a service account JSON key") from None
    return acct


_play_token: dict[str, tuple[float, str]] = {}


async def play_token() -> str:
    acct = play_account()
    cached = _play_token.get(acct["client_email"])
    if cached and cached[0] > time.time() + 60:
        return cached[1]
    now = int(time.time())
    aud = acct.get("token_uri") or "https://oauth2.googleapis.com/token"
    assertion = _jwt({"alg": "RS256", "typ": "JWT"},
                     {"iss": acct["client_email"], "scope": PLAY_SCOPE, "aud": aud, "iat": now, "exp": now + 3600},
                     pem(acct["private_key"]), "RS256")
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(aud, data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion})
    if r.status_code != 200:
        raise ToolError(f"Google refused the service account ({r.status_code}): {r.text[:300]}")
    token = r.json()["access_token"]
    _play_token[acct["client_email"]] = (now + int(r.json().get("expires_in", 3600)), token)
    return token


# ------------------------------------------------------------------------------------------ approvals
async def _approve(action: str, details: str) -> None:
    approved, note = await get_ctx().request_approval(action, agent=get_agent_id(), data={"details": details})
    if not approved:
        raise ToolError("The human didn't approve this" + (f": {note}" if note else "."))


# Read-only or configuration-only subcommands in families that otherwise publish.
_EAS_SAFE = {"update:list", "update:view", "update:configure", "channel:list", "channel:view", "channel:create",
             "workflow:validate", "workflow:runs", "workflow:view", "workflow:logs"}
# Sign in/out (Todd manages the account) and interactive credential managers (they can print keystore passwords).
_EAS_BLOCKED = ("login", "logout", "account:login", "account:logout", "credentials")


def eas_publishes(argv: list[str]) -> str | None:
    """What an EAS command would put in front of people (needs approval), or None."""
    sub = argv[0].lower()
    if sub in _EAS_SAFE:
        return None
    if sub.startswith("submit"):
        return "Submit a build to the App Store / Google Play"
    if sub.startswith(("update", "channel:")):
        return "Publish an over-the-air update to the app's users"
    if sub.startswith("build") and any(a == "--auto-submit" or a.startswith("--auto-submit") for a in argv):
        return "Build the app and submit it to the store"
    if sub == "deploy" and any(a in ("--prod", "--production") for a in argv):
        return "Deploy the app's website to production"
    if sub.startswith("workflow:run"):
        return "Run an EAS workflow (it may build, submit or publish)"
    if sub.startswith("metadata:push"):
        return "Update the app's App Store listing"
    return None


# ------------------------------------------------------------------------------------------ tools
@todd_tool(toolset="mobile")
async def eas(command: str, path: str = ".", timeout_s: int = 900) -> dict:
    """Run Expo's EAS CLI in an app directory, signed in with the human's Expo account. Builds run on Expo's servers
    (iOS on their Macs), so no Xcode or Android SDK is needed here. When they're in the vault, the App Store Connect
    API key (iOS signing, TestFlight) and the Google Play service account (Play uploads) are passed to the CLI for
    this command only. Pass --non-interactive to build/submit. Start builds with --no-wait and follow them with
    `build:view <id> --json` (it has the install URL when finished). `submit`, `update` and `build --auto-submit`
    ask the human first. Examples: "init --non-interactive --force", "build --platform android --profile preview
    --non-interactive --no-wait", "build --platform ios --profile production --non-interactive --no-wait",
    "build:view 0f1e… --json", "submit --platform ios --latest --non-interactive".

    Args:
        command: arguments for eas, e.g. "build --platform all --profile preview --non-interactive --no-wait"
        path: app directory relative to the project root (default .)
        timeout_s: timeout in seconds (default 900, max 1800)
    """
    from .sandbox_tools import _abs

    try:
        argv = shlex.split(command)
    except ValueError as e:
        raise ToolError(f"couldn't parse the arguments: {e}") from e
    if argv and argv[0] == "eas":
        argv = argv[1:]
    if not argv:
        raise ToolError("pass the eas arguments, e.g. \"build --platform android --profile preview --no-wait\"")
    sub = argv[0].lower()
    if any(sub == b or sub.startswith(b + ":") for b in _EAS_BLOCKED):
        raise ToolError(f"`eas {sub}` isn't available through Todd (it signs in/out or manages credentials "
                        "interactively). Todd signs EAS in with EXPO_TOKEN; iOS signing uses the App Store Connect "
                        "key from the vault.")
    token = vault.get_secret(EXPO_TOKEN)
    if not token:
        raise ToolError(NO_EXPO)
    cwd = _abs(path)
    action = eas_publishes(argv)
    if action:
        await _approve(action, f"eas {shlex.join(argv)}\n(in {path})")

    # secrets go in the environment (the sandbox redacts those from output); ids aren't secret, so in the script
    env = {"EXPO_TOKEN": token}
    ids: dict[str, str] = {}
    key_id, issuer, key = (vault.get_secret(n) for n in (ASC_KEY_ID, ASC_ISSUER_ID, ASC_PRIVATE_KEY))
    if key_id and issuer and key:
        env["TODD_ASC_P8"] = pem(key)
        ids |= {"EXPO_ASC_KEY_ID": key_id, "EXPO_ASC_ISSUER_ID": issuer}
    if team := vault.get_secret(APPLE_TEAM_ID):
        ids["EXPO_APPLE_TEAM_ID"] = team
    if play := vault.get_secret(PLAY_SERVICE_ACCOUNT):
        env["TODD_PLAY_JSON"] = play
    script = "\n".join([
        *(f"export {k}={shlex.quote(v)}" for k, v in ids.items()),
        # keys live in a private dir for this command only
        'KEYS=$(mktemp -d /tmp/todd-eas-XXXXXX) && chmod 700 "$KEYS" || exit 1',
        'trap \'rm -rf "$KEYS"\' EXIT',
        'if [ -n "$TODD_ASC_P8" ]; then printf %s "$TODD_ASC_P8" > "$KEYS/asc.p8"; '
        'export EXPO_ASC_API_KEY_PATH="$KEYS/asc.p8"; fi',
        # eas.json reads it through "serviceAccountKeyPath": "$TODD_PLAY_KEY_PATH" (EAS expands env vars there)
        'if [ -n "$TODD_PLAY_JSON" ]; then printf %s "$TODD_PLAY_JSON" > "$KEYS/play.json"; '
        'export TODD_PLAY_KEY_PATH="$KEYS/play.json"; fi',
        "unset TODD_ASC_P8 TODD_PLAY_JSON",
        "export EXPO_NO_TELEMETRY=1",
        # EAS uploads the project from git; outside a repo it archives the directory instead
        "git rev-parse --is-inside-work-tree >/dev/null 2>&1 || export EAS_NO_VCS=1",
        'EAS=eas; command -v eas >/dev/null || EAS="npx -y eas-cli@latest"',
        f"$EAS {shlex.join(argv)} < /dev/null 2>&1",
    ])
    r = await sandbox.exec_(script, cwd=cwd, timeout=int(min(max(timeout_s, 10), 1800)), env=env)
    out = r.get("output") or ""
    for v in env.values():
        if len(v) >= 8:
            out = out.replace(v, "***")
    res: dict[str, Any] = {"exit_code": r.get("exit_code"), "output": out, "timed_out": r.get("timed_out", False)}
    if "Credentials are not set up" in out or "again in interactive mode" in out:
        res["next_step"] = first_ios_credentials_note(cwd)
    return res


def first_ios_credentials_note(cwd: str) -> str:
    """EAS never creates an iOS distribution certificate in non-interactive mode: that one step needs a person."""
    return ("This app has no iOS distribution certificate on EAS yet, and EAS only creates one interactively (once per "
            "Apple team). Ask the human to run this once in a terminal on the Todd machine, choosing the defaults "
            "(it signs in to Apple with their Apple ID and 2FA), then retry the build:\n"
            # logged out afterwards: a session left in the sandbox would let any shell command act as them
            f"docker compose exec -it -w {shlex.quote(cwd)} sandbox bash -lc 'eas login && eas credentials -p ios; "
            "eas logout'")


# Requests that reach people: App Review, releases, testers, public TestFlight links, replies to customer reviews.
_ASC_PUBLISH = re.compile(r"reviewSubmission|appStoreVersionSubmission|appStoreVersionReleaseRequest|"
                          r"betaAppReviewSubmission|betaTesterInvitation|appStoreVersionPhasedRelease|"
                          r"betaTesters|betaGroups/[^/]+/relationships/(betaTesters|builds)|publicLink|"
                          r"customerReviewResponse", re.I)


def _api_path(path: str) -> str:
    path = "/" + path.strip().lstrip("/")
    if ".." in path or "://" in path or "@" in path:
        raise ToolError("path must be an API path like /v1/apps")
    return path


@todd_tool(toolset="mobile")
async def app_store_connect(method: str, path: str, body: dict | None = None, params: dict[str, str] | None = None,
                            max_chars: int = 20000) -> dict:
    """Call the App Store Connect API with the human's API key (a token is signed for each call; the key never
    leaves Todd). Useful: GET /v1/apps (the app's id, which `eas submit` needs as ascAppId), GET /v1/builds with
    params {"filter[app]": id, "sort": "-uploadedDate"} (TestFlight processing state), GET /v1/betaGroups,
    GET /v1/apps/{id}/appStoreVersions. Anything that goes out to people (review submissions, releases, tester
    invitations, adding testers or builds to groups, public TestFlight links, replies to reviews) asks the human first. Creating a new app record isn't in the API:
    do that in App Store Connect with the browser.

    Args:
        method: GET, POST, PATCH or DELETE
        path: API path, e.g. /v1/apps
        body: JSON:API request body for POST/PATCH
        params: query parameters, e.g. {"filter[bundleId]": "com.example.app"}
        max_chars: maximum characters of the response to return
    """
    method = method.upper()
    if method not in {"GET", "POST", "PATCH", "DELETE"}:
        raise ToolError("method must be GET, POST, PATCH or DELETE")
    path = _api_path(path)
    if method != "GET" and _ASC_PUBLISH.search(path + json.dumps(body or {})):
        await _approve(f"App Store Connect: {method} {path}", json.dumps(body or {}, indent=1)[:4000])
    async with httpx.AsyncClient(base_url=ASC_API, timeout=60) as c:
        r = await c.request(method, path, params=params, json=body,
                            headers={"Authorization": f"Bearer {asc_token()}"})
    return _response(r, max_chars)


@todd_tool(toolset="mobile")
async def google_play(method: str, path: str, body: dict | None = None, params: dict[str, str] | None = None,
                      max_chars: int = 20000) -> dict:
    """Call the Google Play Developer API (androidpublisher v3) with the human's service account (the key stays in
    Todd). Changes happen inside an edit: POST applications/{package}/edits → change tracks/listings under
    edits/{editId}/… → POST applications/{package}/edits/{editId}:commit, which publishes them and asks the human
    first (as do replies to user reviews). Uploading a build is easier with `eas submit`. The app itself must first be created in Play Console (the
    browser); the API can't create apps.

    Args:
        method: GET, POST, PUT, PATCH or DELETE
        path: path under androidpublisher/v3/, e.g. applications/com.example.app/edits
        body: JSON request body
        params: query parameters
        max_chars: maximum characters of the response to return
    """
    method = method.upper()
    if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
        raise ToolError("method must be GET, POST, PUT, PATCH or DELETE")
    path = _api_path(path).lstrip("/")
    if method != "GET" and re.search(r":(commit|reply)\b", path):  # publishing an edit; answering a user's review
        await _approve(f"Google Play: {method} {path}", json.dumps(body or {}, indent=1)[:4000])
    async with httpx.AsyncClient(base_url=PLAY_API, timeout=60) as c:
        r = await c.request(method, path, params=params, json=body,
                            headers={"Authorization": f"Bearer {await play_token()}"})
    return _response(r, max_chars)


def _response(r: httpx.Response, max_chars: int) -> dict:
    try:
        text = json.dumps(r.json(), indent=1)
    except Exception:  # noqa: BLE001
        text = r.text
    return {"status": r.status_code, "ok": r.is_success, "body": text[: max(1000, min(int(max_chars), 100000))]}


MOBILE_TOOLS = [eas, app_store_connect, google_play]

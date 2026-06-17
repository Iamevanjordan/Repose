"""Bitwarden SDK integration for Repose OS.

Bitwarden Secrets Manager is the ONLY secrets layer for Repose OS.

HARD RULE (RPOSE-008): there are NO environment-variable fallbacks, NO .env
files, and NO os.environ reads/writes for credentials. If Bitwarden is
unreachable or a secret is missing, every accessor raises immediately so the
caller fails closed — it must never silently degrade to an insecure default.

The only filesystem reference here is the SDK config path, which is a path,
not a secret: the access token and organization id live inside that file (or
the SDK's own state), provisioned out-of-band at deploy time.
"""

import json
import logging
import os.path

logger = logging.getLogger(__name__)

# Location of the Bitwarden SDK config file. This is a filesystem path, not a
# credential. It holds the access_token / organization_id used to authenticate
# the SDK. Provisioned out-of-band; never sourced from process environment.
BW_SDK_CONFIG_PATH = os.path.expanduser("~/.config/bitwarden/sdk-config.json")

_client = None
_org_id: str | None = None
# name -> secret UUID cache (Bitwarden SDK get/create operate on UUIDs)
_id_cache: dict[str, str] = {}
# Resolved project UUID that scopes Repose secrets to the machine account.
_project_id: str | None = None


class BitwardenError(Exception):
    """Raised when Bitwarden operations fail. Always fatal — never caught to
    fall back to env vars or defaults."""


def _load_sdk_config() -> dict:
    """Load the Bitwarden SDK config file. Raises if it cannot be read."""
    try:
        with open(BW_SDK_CONFIG_PATH, "r") as fh:
            return json.load(fh)
    except Exception as e:  # noqa: BLE001 — any failure is fatal, no fallback
        raise BitwardenError(
            f"Bitwarden SDK config unreadable at {BW_SDK_CONFIG_PATH}: {e}. "
            f"Bitwarden is the only secrets layer; refusing to continue."
        ) from e


def _get_client():
    """Initialize and cache the authenticated Bitwarden SDK client.

    Raises BitwardenError immediately on any failure. No fallback path exists.
    """
    global _client, _org_id
    if _client is not None:
        return _client

    cfg = _load_sdk_config()
    access_token = cfg.get("access_token")
    _org_id = cfg.get("organization_id")
    state_path = cfg.get("state_path") or os.path.expanduser(
        "~/.config/bitwarden/sdk-state"
    )
    if not access_token or not _org_id:
        raise BitwardenError(
            "Bitwarden SDK config missing access_token or organization_id."
        )

    try:
        from bitwarden_sdk import BitwardenClient, client_settings_from_dict

        client = BitwardenClient(
            client_settings_from_dict(
                {
                    "apiUrl": cfg.get("api_url", "https://api.bitwarden.com"),
                    "identityUrl": cfg.get(
                        "identity_url", "https://identity.bitwarden.com"
                    ),
                    "deviceType": "SDK",
                    "userAgent": "repose-os",
                }
            )
        )
        client.auth().login_access_token(access_token, state_path)
    except BitwardenError:
        raise
    except Exception as e:  # noqa: BLE001 — fatal, no fallback
        raise BitwardenError(f"Bitwarden SDK initialization failed: {e}") from e

    _client = client
    return _client


def _resolve_secret_id(secret_id: str) -> str | None:
    """Resolve a human-readable secret key to its Bitwarden UUID.

    Returns None if no secret with that key exists. Raises on transport error.
    """
    if secret_id in _id_cache:
        return _id_cache[secret_id]
    client = _get_client()
    try:
        listing = client.secrets().list(_org_id)
        for item in listing.data.data:
            if item.key == secret_id:
                _id_cache[secret_id] = item.id
                return item.id
    except Exception as e:  # noqa: BLE001 — fatal, no fallback
        raise BitwardenError(
            f"Bitwarden secret listing failed while resolving '{secret_id}': {e}"
        ) from e
    return None


def get_secret(secret_id: str) -> str:
    """Resolve a secret from Bitwarden by its secret key.

    Args:
        secret_id: The Bitwarden secret key (e.g., "repose-telegram-bot-token").

    Returns:
        The secret value as a string.

    Raises:
        BitwardenError: If Bitwarden is unreachable or the secret is missing.
                        There is NO environment-variable fallback (RPOSE-008).
    """
    client = _get_client()
    uuid_ = _resolve_secret_id(secret_id)
    if uuid_ is None:
        raise BitwardenError(
            f"Secret '{secret_id}' not found in Bitwarden Secrets Manager."
        )
    try:
        return client.secrets().get(uuid_).data.value
    except Exception as e:  # noqa: BLE001 — fatal, no fallback
        raise BitwardenError(
            f"Bitwarden secret retrieval failed for '{secret_id}': {e}"
        ) from e


def _resolve_project_id() -> str:
    """Resolve the Bitwarden project UUID that scopes Repose secrets.

    The project is identified by *name* (operator-editable, not a secret) under
    ``bitwarden.project_name`` in repose_config.yaml; the UUID is resolved
    through the SDK using the machine-account token so nothing is hardcoded. A
    secret created with an empty project_ids list lands outside the machine
    account's scope and becomes unreachable — this guarantees new secrets are
    placed in the same project as every other Repose secret.

    Raises BitwardenError if the named project cannot be found (never guesses).
    """
    global _project_id
    if _project_id is not None:
        return _project_id
    try:
        from repose.config import repose_config
        project_name = (repose_config.get("bitwarden", {}) or {}).get(
            "project_name", "infra"
        )
    except Exception:  # noqa: BLE001 — config optional; default to the known scope
        project_name = "infra"

    client = _get_client()
    try:
        projects = client.projects().list(_org_id).data.data
    except Exception as e:  # noqa: BLE001 — fatal, no fallback
        raise BitwardenError(
            f"Bitwarden project listing failed while resolving "
            f"'{project_name}': {e}"
        ) from e
    for project in projects:
        if project.name == project_name:
            _project_id = project.id
            return _project_id
    raise BitwardenError(
        f"Bitwarden project '{project_name}' not found; refusing to store a "
        f"secret with empty project scope (it would be unreachable)."
    )


def store_secret(secret_id: str, value: str) -> None:
    """Store (create or update) a secret in Bitwarden Secrets Manager.

    The secret is scoped to the Repose project (resolved via
    _resolve_project_id) so it remains reachable by the machine account.

    Args:
        secret_id: The Bitwarden secret key.
        value: The secret value to store.

    Raises:
        BitwardenError: If the secret cannot be stored. There is NO os.environ
                        write fallback (RPOSE-008) — Bitwarden only.
    """
    client = _get_client()
    note = "Repose OS secret"
    project_ids = [_resolve_project_id()]
    try:
        existing = _resolve_secret_id(secret_id)
        if existing is None:
            client.secrets().create(_org_id, secret_id, value, note, project_ids)
        else:
            client.secrets().update(
                _org_id, existing, secret_id, value, note, project_ids
            )
        logger.info("Secret '%s' stored in Bitwarden.", secret_id)
    except Exception as e:  # noqa: BLE001 — fatal, no fallback
        raise BitwardenError(
            f"Bitwarden secret store failed for '{secret_id}': {e}"
        ) from e

import logging
import json
import uuid
from collections.abc import Mapping
from typing import Any

from gpt_rag_ui.config.appconfig import AppConfigClient


logger = logging.getLogger("gpt_rag_ui.auth_common")


def validated_claims_challenge(value: object) -> str | None:
    """Keep a bounded Entra claims challenge out of diagnostic text."""
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 8192:
        return None
    try:
        claims = json.loads(value)
    except ValueError:
        return None
    if (
        not isinstance(claims, dict)
        or not claims
        or not set(claims).issubset({"access_token", "id_token", "userinfo"})
        or not all(isinstance(item, dict) for item in claims.values())
    ):
        return None
    return value


class DelegationError(RuntimeError):
    """Safe auth recovery outcome; provider bodies and credentials stay private."""

    def __init__(self, code: str, *, claims_challenge: object = None):
        messages = {
            "consent_required": (
                "Consent is required for protected retrieval. Ask your administrator "
                "to grant the application's delegated API permissions, then sign in again."
            ),
            "interaction_required": (
                "Additional sign-in verification is required by your organization's "
                "access policy. Sign out and sign in again to continue."
            ),
            "configuration_error": (
                "Delegated authentication is not configured correctly. "
                "Contact the application administrator."
            ),
            "authentication_unavailable": (
                "The authentication service is temporarily unavailable. Please try again."
            ),
            "source_denied": (
                "Access to the protected source was denied. Check your source permissions "
                "and the application's delegated consent with your administrator."
            ),
            "authentication_required": (
                "Your sign-in must be renewed. Please sign out and sign in again."
            ),
        }
        self.code = code if code in messages else "authentication_required"
        self.claims_challenge = validated_claims_challenge(claims_challenge)
        super().__init__(messages[self.code])

    @classmethod
    def from_msal(cls, result: Mapping[str, Any]) -> "DelegationError":
        error = result.get("error")
        suberror = result.get("suberror")
        codes = result.get("error_codes")
        error_codes = set(item for item in codes if isinstance(item, int)) if isinstance(codes, list) else set()
        if suberror == "consent_required" or 65001 in error_codes:
            code = "consent_required"
        elif (
            error == "interaction_required"
            or (isinstance(suberror, str) and suberror in {"basic_action", "additional_action", "message_only"})
            or error_codes.intersection({50076, 50079, 50158})
            or validated_claims_challenge(result.get("claims"))
        ):
            code = "interaction_required"
        elif isinstance(error, str) and error in {"invalid_client", "unauthorized_client"}:
            code = "configuration_error"
        elif isinstance(error, str) and error in {"temporarily_unavailable", "server_error"}:
            code = "authentication_unavailable"
        else:
            code = "authentication_required"
        return cls(code, claims_challenge=result.get("claims"))


def normalize_guid(value: str, *, claim_name: str) -> str:
    try:
        return str(uuid.UUID(str(value or "").strip()))
    except (AttributeError, ValueError) as exc:
        raise ValueError(f"{claim_name} must be a GUID.") from exc


def canonical_principal_id(tenant_id: str, object_id: str) -> str:
    tenant = normalize_guid(tenant_id, claim_name="tid")
    principal = normalize_guid(object_id, claim_name="oid")
    return f"{tenant}:{principal}"


def _read_list(config: AppConfigClient, key: str) -> list[str]:
    value = config.get(key, "", str) or ""
    return [item.strip() for item in value.split(",") if item.strip()]


def is_user_authorized(
    config: AppConfigClient,
    principal_name: str,
    principal_id: str,
) -> bool:
    allowed_names = {
        value.casefold() for value in _read_list(config, "ALLOWED_USER_NAMES")
    }
    allowed_ids = {
        value.lower() for value in _read_list(config, "ALLOWED_USER_PRINCIPALS")
    }

    if not (allowed_names or allowed_ids):
        return True

    normalized_principal_id = str(principal_id or "").strip().lower()
    candidate_ids = {normalized_principal_id}
    if ":" in normalized_principal_id:
        _, object_id = normalized_principal_id.split(":", 1)
        if object_id:
            # Existing deployments commonly allow-list a bare oid. Keep
            # accepting it while storing the tenant-qualified identity.
            candidate_ids.add(object_id)

    if (
        str(principal_name or "").strip().casefold() in allowed_names
        or not candidate_ids.isdisjoint(allowed_ids)
    ):
        return True

    logger.warning(
        "Access denied for principal '%s' (%s). No matching allow list entry.",
        principal_name,
        principal_id,
    )
    return False

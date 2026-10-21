import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any

import frappe
from firebase_admin import auth
from frappe import _

from flutter_utils.firebase_tokens import verify_id_token
from flutter_utils.flutter_utils.doctype.firebase_auth_identity.firebase_auth_identity import (
	_build_identity_key,
)

PLACEHOLDER_EMAIL_SUFFIX = "@users.invalid"


class FirebaseAuthError(frappe.AuthenticationError):
	def __init__(self, message: str, error_code: str, http_status_code: int = 401):
		super().__init__(message)
		self.error_code = error_code
		self.http_status_code = http_status_code


@dataclass(frozen=True)
class VerifiedFirebaseIdentity:
	project_id: str
	uid: str
	provider: str
	email: str | None
	phone_number: str | None
	full_name: str | None
	authenticated_at: int | None = None


def parse_service_account_json(raw_credentials: str | None) -> dict[str, Any]:
	if not raw_credentials:
		frappe.throw(_("Firebase Service Account JSON is required."))

	try:
		service_account = json.loads(raw_credentials)
	except TypeError, json.JSONDecodeError:
		frappe.throw(_("Firebase Service Account JSON must contain valid JSON."))

	if not isinstance(service_account, dict) or service_account.get("type") != "service_account":
		frappe.throw(_("Firebase credentials must be a service-account JSON object."))

	required_fields = ("project_id", "private_key", "client_email", "token_uri")
	if any(not service_account.get(field) for field in required_fields):
		frappe.throw(_("Firebase Service Account JSON is missing required fields."))

	return service_account


def get_firebase_app(settings: Any | None = None):
	from flutter_utils.firebase import get_firebase_app as initialize_configured_app

	settings = settings or _get_enabled_firebase_settings()
	try:
		return initialize_configured_app(settings)
	except (TypeError, ValueError) as exc:
		raise FirebaseAuthError(
			_("Firebase Authentication is not configured correctly."),
			"firebase_configuration_error",
			503,
		) from exc


def verify_firebase_id_token(id_token: str) -> VerifiedFirebaseIdentity:
	if not id_token or not id_token.strip():
		raise FirebaseAuthError(_("Firebase ID token is required."), "firebase_token_required")

	settings = _get_enabled_firebase_settings()
	app = get_firebase_app(settings)
	started = time.monotonic()
	result = "failed"
	try:
		claims = verify_id_token(
			id_token.strip(),
			app=app,
			check_revoked=bool(settings.firebase_check_revoked_tokens),
		)
		result = "ok"
	except auth.ExpiredIdTokenError as exc:
		raise FirebaseAuthError(
			_("Firebase session has expired. Please sign in again."), "firebase_token_expired"
		) from exc
	except auth.RevokedIdTokenError as exc:
		raise FirebaseAuthError(
			_("Firebase session has been revoked. Please sign in again."), "firebase_token_revoked"
		) from exc
	except auth.UserDisabledError as exc:
		raise FirebaseAuthError(
			_("This Firebase account has been disabled."), "firebase_user_disabled", 403
		) from exc
	except (auth.InvalidIdTokenError, ValueError) as exc:
		raise FirebaseAuthError(
			_("Firebase authentication token is invalid."), "firebase_token_invalid"
		) from exc
	except Exception as exc:
		frappe.log_error(
			title="Firebase Token Verification Failed",
			message=f"Verification unavailable ({type(exc).__name__}). See firebase_auth timing logs.",
		)
		raise FirebaseAuthError(
			_("Firebase Authentication is temporarily unavailable."),
			"firebase_unavailable",
			503,
		) from exc
	finally:
		logger = frappe.logger("firebase_auth", allow_site=True)
		logger.setLevel("INFO")
		logger.info(
			json.dumps(
				{
					"stage": "token_verification",
					"result": result,
					"seconds": round(time.monotonic() - started, 3),
				}
			)
		)

	project_id = settings.firebase_project_id.strip()
	if claims.get("aud") != project_id:
		raise FirebaseAuthError(
			_("Firebase token belongs to a different project."), "firebase_project_mismatch"
		)

	uid = str(claims.get("uid") or claims.get("sub") or "").strip()
	if not uid:
		raise FirebaseAuthError(
			_("Firebase token does not contain a user identity."), "firebase_token_invalid"
		)

	provider = str((claims.get("firebase") or {}).get("sign_in_provider") or "").strip()
	_validate_provider(settings, provider)

	email = _normalize_email(claims.get("email"))
	phone_number = _normalize_phone(claims.get("phone_number"))
	if provider == "google.com" and (not email or claims.get("email_verified") is not True):
		raise FirebaseAuthError(_("Google account email is not verified."), "firebase_email_not_verified")
	if provider == "phone" and not phone_number:
		raise FirebaseAuthError(_("Firebase phone number is missing or invalid."), "firebase_phone_invalid")

	return VerifiedFirebaseIdentity(
		project_id=project_id,
		uid=uid,
		provider=provider,
		email=email,
		phone_number=phone_number,
		full_name=_normalize_name(claims.get("name")),
		authenticated_at=_normalize_auth_time(claims.get("auth_time")),
	)


def resolve_firebase_user(identity: VerifiedFirebaseIdentity) -> Any:
	identity_name = _build_identity_key(identity.project_id, identity.uid)
	if frappe.db.exists("Firebase Auth Identity", identity_name):
		mapping = frappe.get_doc("Firebase Auth Identity", identity_name)
		user = _get_enabled_user(mapping.user, identity)
		_update_identity_mapping(mapping, identity)
		_sync_verified_identity_to_user(user, identity)
		return user

	user = _find_existing_user(identity, allow_reactivation=True)
	settings = _get_enabled_firebase_settings()
	if not user:
		if not settings.firebase_auto_create_users:
			raise FirebaseAuthError(
				_("No account is linked to this Firebase identity."),
				"firebase_user_not_found",
				403,
			)
		user = _create_website_user(identity)

	_insert_identity_mapping(user.name, identity)
	_sync_verified_identity_to_user(user, identity)
	return user


def get_verified_user_contacts(user_name: str) -> tuple[str | None, str | None]:
	"""Return the latest verified Firebase email and phone for a Frappe user."""
	identities = frappe.get_all(
		"Firebase Auth Identity",
		filters={"user": user_name},
		fields=["email", "phone_number"],
		order_by="modified desc",
	)
	email = next(
		(
			identity.email
			for identity in identities
			if identity.email and not identity.email.lower().endswith(PLACEHOLDER_EMAIL_SUFFIX)
		),
		None,
	)
	phone_number = next((identity.phone_number for identity in identities if identity.phone_number), None)
	return email, phone_number


def validate_recent_firebase_authentication(
	identity: VerifiedFirebaseIdentity, max_age_seconds: int = 300
) -> None:
	now = int(time.time())
	if (
		identity.authenticated_at is None
		or identity.authenticated_at > now + 60
		or now - identity.authenticated_at > max_age_seconds
	):
		raise FirebaseAuthError(
			_("Please sign in again before linking accounts."),
			"firebase_recent_login_required",
			401,
		)


def link_firebase_identities(
	primary_identity: VerifiedFirebaseIdentity,
	secondary_identity: VerifiedFirebaseIdentity,
) -> Any:
	if primary_identity.project_id != secondary_identity.project_id:
		raise FirebaseAuthError(
			_("Firebase identities belong to different projects."),
			"firebase_project_mismatch",
			409,
		)
	if primary_identity.uid == secondary_identity.uid:
		raise FirebaseAuthError(
			_("These sign-in methods already belong to the same Firebase account."),
			"firebase_identities_already_linked",
			409,
		)

	primary_mapping = _get_identity_mapping(primary_identity)
	secondary_mapping = _get_identity_mapping(secondary_identity)
	user = _resolve_link_target(
		primary_identity,
		secondary_identity,
		primary_mapping,
		secondary_mapping,
	)

	for identity, mapping in (
		(primary_identity, primary_mapping),
		(secondary_identity, secondary_mapping),
	):
		if mapping:
			_update_identity_mapping(mapping, identity)
		else:
			_insert_identity_mapping(user.name, identity)
		_sync_verified_identity_to_user(user, identity)

	return user


def test_firebase_connection() -> dict[str, str]:
	settings = _get_enabled_firebase_settings()
	try:
		next(auth.list_users(max_results=1, app=get_firebase_app(settings)).iterate_all(), None)
	except Exception as exc:
		frappe.log_error(
			title="Firebase Connection Test Failed", message=frappe.get_traceback(with_context=False)
		)
		raise FirebaseAuthError(
			_("Could not connect to Firebase Authentication with the configured credentials."),
			"firebase_connection_failed",
			503,
		) from exc
	return {"message": _("Firebase Authentication connection succeeded.")}


def _get_enabled_firebase_settings():
	settings = frappe.get_cached_doc("Flutter Utils Settings")
	if not settings.enable_firebase_auth:
		raise FirebaseAuthError(_("Firebase Authentication is disabled."), "firebase_auth_disabled", 403)
	return settings


def _validate_provider(settings: Any, provider: str) -> None:
	if provider == "phone" and settings.enable_firebase_phone_auth:
		return
	if provider == "google.com" and settings.enable_firebase_google_auth:
		return
	if provider not in {"phone", "google.com"}:
		raise FirebaseAuthError(
			_("Firebase sign-in provider is not supported."), "firebase_provider_unsupported", 403
		)
	provider_label = _("Phone") if provider == "phone" else _("Google")
	message = _("Firebase {0} authentication is disabled.").format(provider_label)
	code = "firebase_phone_disabled" if provider == "phone" else "firebase_google_disabled"
	raise FirebaseAuthError(message, code, 403)


def _find_existing_user(identity: VerifiedFirebaseIdentity, *, allow_reactivation: bool = False):
	if identity.provider == "google.com" and identity.email:
		if frappe.db.exists("User", identity.email):
			return _get_enabled_user(
				identity.email, identity if allow_reactivation else None
			)
		return None

	if identity.provider == "phone" and identity.phone_number:
		users = frappe.get_all(
			"User",
			filters={"mobile_no": identity.phone_number},
			fields=["name", "enabled"],
			limit=2,
		)
		if len(users) > 1:
			raise FirebaseAuthError(
				_("Multiple accounts use this phone number. Please contact support."),
				"firebase_phone_conflict",
				409,
			)
		if users:
			return _get_enabled_user(
				users[0].name, identity if allow_reactivation else None
			)
	return None


def _get_identity_mapping(identity: VerifiedFirebaseIdentity) -> Any | None:
	identity_name = _build_identity_key(identity.project_id, identity.uid)
	if not frappe.db.exists("Firebase Auth Identity", identity_name):
		return None
	return frappe.get_doc("Firebase Auth Identity", identity_name)


def _resolve_link_target(
	primary_identity: VerifiedFirebaseIdentity,
	secondary_identity: VerifiedFirebaseIdentity,
	primary_mapping: Any | None,
	secondary_mapping: Any | None,
) -> Any:
	mapped_users = {mapping.user for mapping in (primary_mapping, secondary_mapping) if mapping}
	if len(mapped_users) > 1:
		_raise_manual_merge_required()

	if mapped_users:
		user = _get_enabled_user(mapped_users.pop())
		for identity, mapping in (
			(primary_identity, primary_mapping),
			(secondary_identity, secondary_mapping),
		):
			if mapping:
				continue
			matched_user = _find_existing_user(identity)
			if matched_user and matched_user.name != user.name:
				_raise_manual_merge_required()
		return user

	candidates = {
		candidate.name: candidate
		for candidate in (
			_find_existing_user(primary_identity),
			_find_existing_user(secondary_identity),
		)
		if candidate
	}
	if len(candidates) > 1:
		_raise_manual_merge_required()
	if candidates:
		return next(iter(candidates.values()))

	settings = _get_enabled_firebase_settings()
	if not settings.firebase_auto_create_users:
		raise FirebaseAuthError(
			_("No account is linked to either Firebase identity."),
			"firebase_user_not_found",
			403,
		)
	preferred_identity = primary_identity
	if not preferred_identity.email and secondary_identity.email:
		preferred_identity = secondary_identity
	return _create_website_user(preferred_identity)


def _raise_manual_merge_required() -> None:
	raise FirebaseAuthError(
		_("These Firebase identities belong to different accounts. Please contact support."),
		"firebase_accounts_require_manual_merge",
		409,
	)


def _get_enabled_user(user_name: str, identity: VerifiedFirebaseIdentity | None = None):
	if not frappe.db.exists("User", user_name):
		raise FirebaseAuthError(_("The linked account no longer exists."), "firebase_user_not_found", 403)
	if not frappe.db.get_value("User", user_name, "enabled"):
		if identity is not None and (user := _reactivate_disabled_user(user_name, identity)):
			return user
		raise FirebaseAuthError(_("Your account has been disabled."), "frappe_user_disabled", 403)
	return frappe.get_doc("User", user_name)


def _reactivate_disabled_user(
	user_name: str, identity: VerifiedFirebaseIdentity
) -> Any | None:
	"""Give the owning app a chance to reopen a self-deleted identity.

	Apps register ``firebase_user_reactivation`` hooks. A hook returns the user
	when the account may rejoin and ``None`` when it must stay blocked (admin
	disable, fraud block or no deletion marker). Hook failures are logged and
	treated as "stay blocked" so a broken integration never grants access.
	"""
	for method in frappe.get_hooks("firebase_user_reactivation") or []:
		try:
			user = frappe.get_attr(method)(user_name=user_name, identity=identity)
		except frappe.AuthenticationError:
			raise
		except Exception:
			frappe.log_error(
				title="Firebase user reactivation failed",
				message=frappe.get_traceback(with_context=False),
			)
			continue
		if user:
			return frappe.get_doc("User", user if isinstance(user, str) else user.name)
	return None


def _create_website_user(identity: VerifiedFirebaseIdentity):
	email = identity.email or _build_phone_user_email(identity)
	user = frappe.new_doc("User")
	user.email = email
	user.first_name = identity.full_name or _("Firebase User")
	user.mobile_no = identity.phone_number
	user.enabled = 1
	user.user_type = "Website User"
	user.new_password = frappe.generate_hash(length=32)
	user.send_welcome_email = 0
	user.insert(ignore_permissions=True)
	return user


def _build_phone_user_email(identity: VerifiedFirebaseIdentity) -> str:
	digest = hashlib.sha256(f"{identity.project_id}:{identity.uid}".encode()).hexdigest()[:32]
	return f"firebase-{digest}@users.invalid"


def _insert_identity_mapping(user_name: str, identity: VerifiedFirebaseIdentity) -> Any:
	mapping = frappe.get_doc(
		{
			"doctype": "Firebase Auth Identity",
			"firebase_project_id": identity.project_id,
			"firebase_uid": identity.uid,
			"user": user_name,
			"last_sign_in_provider": identity.provider,
			"phone_number": identity.phone_number,
			"email": identity.email,
		}
	)
	mapping.insert(ignore_permissions=True)
	return mapping


def _sync_verified_identity_to_user(user: Any, identity: VerifiedFirebaseIdentity) -> None:
	if identity.phone_number and user.mobile_no != identity.phone_number:
		user.mobile_no = identity.phone_number
		user.save(ignore_permissions=True)


def _update_identity_mapping(mapping: Any, identity: VerifiedFirebaseIdentity) -> None:
	updates = {
		"last_sign_in_provider": identity.provider,
		"phone_number": identity.phone_number,
		"email": identity.email,
	}
	if any(mapping.get(fieldname) != value for fieldname, value in updates.items()):
		mapping.update(updates)
		mapping.save(ignore_permissions=True)


def _normalize_email(value: Any) -> str | None:
	if not isinstance(value, str) or not value.strip():
		return None
	return value.strip().lower()


def _normalize_phone(value: Any) -> str | None:
	if not isinstance(value, str) or not value.strip():
		return None
	from flutter_utils.api.auth import normalize_mobile_number

	try:
		return normalize_mobile_number(value)
	except frappe.ValidationError as exc:
		raise FirebaseAuthError(_("Firebase phone number is invalid."), "firebase_phone_invalid") from exc


def _normalize_name(value: Any) -> str | None:
	if not isinstance(value, str) or not value.strip():
		return None
	return value.strip()[:140]


def _normalize_auth_time(value: Any) -> int | None:
	if isinstance(value, bool) or not isinstance(value, int | float):
		return None
	return int(value)

from dataclasses import dataclass

import frappe
from frappe import _


@dataclass(frozen=True)
class AuthenticatedIdentity:
	"""Verified request identity. Never store credentials or tokens here."""

	user: str
	method: str
	device_credential: str | None = None


def clear_authenticated_identity() -> None:
	frappe.local.flutter_utils_identity = None


def get_authenticated_identity() -> AuthenticatedIdentity | None:
	identity = getattr(frappe.local, "flutter_utils_identity", None)
	if identity and identity.user == frappe.session.user:
		return identity
	return None


def apply_authenticated_identity(identity: AuthenticatedIdentity, *, set_user: bool = True) -> None:
	if set_user:
		form_dict = frappe.local.form_dict
		frappe.set_user(identity.user)
		frappe.local.form_dict = form_dict
	elif identity.user != frappe.session.user:
		raise frappe.AuthenticationError
	frappe.local.flutter_utils_identity = identity


def require_current_device_credential() -> str:
	"""Reverify ownership/status so revocation within a request cannot use stale context."""
	from flutter_utils.authentication.device import AUTHORIZATION_SOURCE, authenticate_device_credentials
	from flutter_utils.authentication.headers import parse_authorization_header

	clear_authenticated_identity()
	if frappe.session.user in ("", "Guest"):
		raise frappe.AuthenticationError
	authorization = parse_authorization_header()
	if frappe.get_request_header("Frappe-Authorization-Source", "") != AUTHORIZATION_SOURCE:
		frappe.throw(_("This credential is not a managed device login."), frappe.AuthenticationError)
	if authorization.scheme != "token":
		# Device APIs retain their existing HTTP contract, not Firebase/cookie authentication.
		raise frappe.AuthenticationError
	identity = authenticate_device_credentials(authorization.credentials)
	apply_authenticated_identity(identity, set_user=False)
	return str(identity.device_credential)

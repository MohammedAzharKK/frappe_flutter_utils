import hmac

import frappe
from frappe.utils.password import get_decrypted_password

from flutter_utils.authentication.context import AuthenticatedIdentity
from flutter_utils.authentication.headers import parse_device_credentials

AUTHORIZATION_SOURCE = "Flutter Device Credential"


def authenticate_device_credentials(credentials: str) -> AuthenticatedIdentity:
	"""Verify device and user status for both native HTTP and realtime adapters."""
	api_key, api_secret = parse_device_credentials(credentials)
	credential = frappe.db.get_value(
		AUTHORIZATION_SOURCE,
		{"api_key": api_key, "enabled": 1},
		["name", "user"],
		as_dict=True,
	)
	if not credential or not frappe.db.get_value("User", credential.user, "enabled"):
		raise frappe.AuthenticationError
	secret = get_decrypted_password(
		AUTHORIZATION_SOURCE, credential.name, "api_secret", raise_exception=False
	)
	if not secret or not hmac.compare_digest(secret.encode(), api_secret.encode()):
		raise frappe.AuthenticationError
	return AuthenticatedIdentity(
		user=credential.user, method="managed_device", device_credential=credential.name
	)

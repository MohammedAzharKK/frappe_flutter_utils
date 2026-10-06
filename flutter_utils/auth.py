import frappe

from flutter_utils.authentication.context import (
	AuthenticatedIdentity,
	apply_authenticated_identity,
	clear_authenticated_identity,
)
from flutter_utils.authentication.device import AUTHORIZATION_SOURCE, authenticate_device_credentials
from flutter_utils.authentication.headers import parse_authorization_header


def validate() -> None:
	"""Adapt supported authorization schemes to a verified request-local identity."""
	clear_authenticated_identity()
	authorization = parse_authorization_header()
	if authorization.scheme == "flutterdevice":
		apply_authenticated_identity(authenticate_device_credentials(authorization.credentials))
		return
	if authorization.scheme == "token":
		if frappe.get_request_header("Frappe-Authorization-Source", "") == AUTHORIZATION_SOURCE:
			# Native Frappe authentication runs first. Do not replace its user resolution.
			identity = authenticate_device_credentials(authorization.credentials)
			apply_authenticated_identity(identity, set_user=False)
		return
	if authorization.scheme != "firebase":
		return
	if not authorization.credentials.strip():
		raise frappe.AuthenticationError

	from flutter_utils.firebase_auth import resolve_firebase_user, verify_firebase_id_token

	user = resolve_firebase_user(verify_firebase_id_token(authorization.credentials.strip()))
	apply_authenticated_identity(AuthenticatedIdentity(user=user.name, method="firebase"))

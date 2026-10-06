import frappe

from flutter_utils.authentication.context import apply_authenticated_identity
from flutter_utils.authentication.device import authenticate_device_credentials
from flutter_utils.authentication.headers import parse_authorization_header


@frappe.whitelist()
def get_device_context() -> dict[str, str]:
	"""Reverify a Socket.IO connection after its revocation listener is installed."""
	authorization = parse_authorization_header()
	if authorization.scheme != "flutterdevice":
		raise frappe.AuthenticationError
	identity = authenticate_device_credentials(authorization.credentials)
	apply_authenticated_identity(identity, set_user=False)
	return {"user": identity.user, "credential": str(identity.device_credential)}

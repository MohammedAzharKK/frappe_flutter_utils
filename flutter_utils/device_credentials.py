import hashlib
from typing import Any

import frappe
from frappe import _
from frappe.utils import now_datetime
from frappe.utils.password import set_encrypted_password

from flutter_utils.authentication.context import require_current_device_credential
from flutter_utils.authentication.device import AUTHORIZATION_SOURCE

MAX_DEVICE_ID_LENGTH = 128
MAX_DEVICE_NAME_LENGTH = 140


def issue_device_api_credentials(
	user: Any,
	device_id: str,
	device_name: str | None = None,
) -> dict[str, Any]:
	device_id_hash = hash_device_id(device_id)
	clean_device_name = normalize_device_name(device_name)

	frappe.db.get_value("User", user.name, "name", for_update=True)
	if not frappe.db.get_value("User", user.name, "enabled"):
		frappe.throw(_("Your account has been disabled. Please contact the administrator."))

	credentials = frappe.get_all(
		AUTHORIZATION_SOURCE,
		filters={"user": user.name},
		fields=["name", "device_id_hash", "enabled", "last_login_at", "creation"],
		order_by="last_login_at asc, creation asc",
	)
	existing = next((row for row in credentials if row.device_id_hash == device_id_hash), None)

	if not existing or not existing.enabled:
		_revoke_for_available_slot(credentials, _get_maximum_logged_in_devices())

	api_secret = frappe.generate_hash(length=32)
	now = now_datetime()
	if existing:
		from flutter_utils.realtime import disconnect_rotated_device_sockets

		disconnect_rotated_device_sockets(existing.name)
		credential = frappe.get_doc(AUTHORIZATION_SOURCE, existing.name)
		credential.device_name = clean_device_name
		credential.enabled = 1
		credential.last_login_at = now
		credential.revoked_at = None
		credential.revocation_reason = None
		set_encrypted_password(AUTHORIZATION_SOURCE, credential.name, api_secret, "api_secret")
		credential.save(ignore_permissions=True)
	else:
		credential = frappe.get_doc(
			{
				"doctype": AUTHORIZATION_SOURCE,
				"user": user.name,
				"device_id_hash": device_id_hash,
				"device_key": _build_device_key(user.name, device_id_hash),
				"device_name": clean_device_name,
				"api_key": frappe.generate_hash(length=32),
				"api_secret": api_secret,
				"enabled": 1,
				"last_login_at": now,
			}
		).insert(ignore_permissions=True)
		if len(credentials) == 0:
			_invalidate_legacy_user_secret(user.name)

	return {
		"api_key": credential.api_key,
		"api_secret": api_secret,
		"authorization_source": AUTHORIZATION_SOURCE,
		"full_name": user.full_name,
		"email": user.email,
		"mobile_no": user.mobile_no,
	}


def hash_device_id(device_id: str | None) -> str:
	if not isinstance(device_id, str):
		frappe.throw(_("Device ID is required for token login."))
	clean_device_id = device_id.strip()
	if not clean_device_id:
		frappe.throw(_("Device ID is required for token login."))
	if len(clean_device_id) > MAX_DEVICE_ID_LENGTH:
		frappe.throw(_("Device ID is invalid."))
	return hashlib.sha256(clean_device_id.encode()).hexdigest()


def normalize_device_name(device_name: str | None) -> str | None:
	if device_name is None:
		return None
	if not isinstance(device_name, str):
		frappe.throw(_("Device name is invalid."))
	clean_device_name = device_name.strip()
	if len(clean_device_name) > MAX_DEVICE_NAME_LENGTH:
		frappe.throw(_("Device name is invalid."))
	return clean_device_name or None


def logout_current_device() -> dict[str, str]:
	credential_name = get_current_device_credential()
	_revoke_credential(credential_name, "Device Logout")
	return {"message": _("Device logged out successfully.")}


def get_current_device_credential() -> str:
	"""Compatibility wrapper for logout and push registration consumers."""
	return require_current_device_credential()


def prune_device_credentials(maximum_devices: int) -> None:
	maximum_devices = max(1, int(maximum_devices))
	users = frappe.get_all(
		AUTHORIZATION_SOURCE,
		filters={"enabled": 1},
		pluck="user",
		order_by="user asc",
	)
	for user in dict.fromkeys(users):
		frappe.db.get_value("User", user, "name", for_update=True)
		credentials = frappe.get_all(
			AUTHORIZATION_SOURCE,
			filters={"user": user, "enabled": 1},
			fields=["name"],
			order_by="last_login_at desc, creation desc",
		)
		for credential in credentials[maximum_devices:]:
			_revoke_credential(credential.name, "Limit Reduced")
		frappe.db.commit()


def _get_maximum_logged_in_devices() -> int:
	value = frappe.db.get_single_value("Flutter Utils Settings", "maximum_logged_in_devices")
	return max(1, int(value or 1))


def _revoke_for_available_slot(credentials: list[Any], maximum_devices: int) -> None:
	active = [credential for credential in credentials if credential.enabled]
	to_revoke = max(0, len(active) - maximum_devices + 1)
	for credential in active[:to_revoke]:
		_revoke_credential(credential.name, "Device Limit")


def _revoke_credential(credential_name: str, reason: str) -> None:
	from flutter_utils.push_notifications import deactivate_device_push
	from flutter_utils.realtime import disconnect_device_sockets

	frappe.db.get_value(AUTHORIZATION_SOURCE, credential_name, "name", for_update=True)
	deactivate_device_push(credential_name)
	frappe.db.set_value(
		AUTHORIZATION_SOURCE,
		credential_name,
		{
			"enabled": 0,
			"revoked_at": now_datetime(),
			"revocation_reason": reason,
		},
		update_modified=False,
	)
	api_key = frappe.db.get_value(AUTHORIZATION_SOURCE, credential_name, "api_key")
	disconnect_device_sockets(api_key)


def _build_device_key(user: str, device_id_hash: str) -> str:
	return hashlib.sha256(f"{user}\0{device_id_hash}".encode()).hexdigest()


def _invalidate_legacy_user_secret(user: str) -> None:
	if frappe.db.get_value("User", user, "api_key"):
		set_encrypted_password("User", user, frappe.generate_hash(length=32), "api_secret")

"""Reusable managed-device FCM registration and background delivery."""

import hashlib
import re
from typing import Any

import frappe
from frappe import _
from frappe.utils import now_datetime

from flutter_utils.device_credentials import AUTHORIZATION_SOURCE, get_current_device_credential

REGISTRATION_DOCTYPE = AUTHORIZATION_SOURCE
VALID_PLATFORMS = {"android", "ios", "web", "macos", "windows", "linux"}

# The app creates this high-importance channel at startup. Android 8+ only
# shows a heads-up tray notification when FCM targets an existing channel, so
# every message names it explicitly instead of the low-importance fallback.
ANDROID_CHANNEL_ID = "sync_updates_v2"
ANDROID_ICON = "ic_launcher"


def _token_hash(token: str) -> str:
	return hashlib.sha256(token.encode()).hexdigest()


@frappe.whitelist(methods=["POST"])
def register_push_device(token: str, platform: str, app: str) -> dict[str, Any]:
	device = get_current_device_credential()
	if not isinstance(token, str) or not token.strip() or len(token) > 4096:
		frappe.throw(_("Device token is invalid."))
	token = token.strip()
	if not isinstance(platform, str) or platform.lower() not in VALID_PLATFORMS:
		frappe.throw(_("Unsupported device platform."))
	if not isinstance(app, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,139}", app):
		frappe.throw(_("Application identifier is invalid."))
	# Serialize token refresh and credential revocation for this device.
	if not frappe.db.get_value("Flutter Device Credential", device, "enabled", for_update=True):
		raise frappe.AuthenticationError
	# A token belongs to one installation/account at a time. Release the old
	# owner before assigning it; the unique hash also guards concurrent claims.
	token_hash = _token_hash(token)
	frappe.db.set_value(
		REGISTRATION_DOCTYPE,
		{"fcm_token_hash": token_hash, "name": ["!=", device]},
		{"fcm_token": None, "fcm_token_hash": None, "push_enabled": 0},
		update_modified=False,
	)
	values = {
		"app": app,
		"platform": platform.lower(),
		"fcm_token": token,
		"fcm_token_hash": token_hash,
		"push_enabled": 1,
		"push_last_seen_at": now_datetime(),
	}
	frappe.db.set_value(REGISTRATION_DOCTYPE, device, values, update_modified=False)
	return {"name": device, "active": True}


@frappe.whitelist(methods=["POST"])
def unregister_push_device(token: str) -> None:
	device = get_current_device_credential()
	if not isinstance(token, str) or not token.strip() or len(token) > 4096:
		frappe.throw(_("Device token is invalid."))
	frappe.db.set_value(
		REGISTRATION_DOCTYPE,
		{
			"name": device,
			"user": frappe.session.user,
			"fcm_token_hash": _token_hash(token.strip()),
		},
		"push_enabled",
		0,
		update_modified=False,
	)


def deactivate_device_push(device: str, app: str | None = None) -> None:
	# Credential issuance can run during upgrades before the new schema is synced.
	if not frappe.db.has_column(REGISTRATION_DOCTYPE, "push_enabled"):
		return
	filters = {"name": device, "push_enabled": 1}
	if app:
		filters["app"] = app
	frappe.db.set_value(REGISTRATION_DOCTYPE, filters, "push_enabled", 0, update_modified=False)


def enqueue_push_notifications(
	users: list[str],
	title: str,
	body: str,
	app: str,
	data: dict[str, str] | None = None,
) -> None:
	if users:
		frappe.enqueue(
			"flutter_utils.push_notifications.send_push_notifications",
			enqueue_after_commit=True,
			users=list(dict.fromkeys(users)),
			title=title,
			body=body,
			app=app,
			data=data,
		)


def send_push_notifications(
	users: list[str],
	title: str,
	body: str,
	app: str | None = None,
	data: dict[str, str] | None = None,
	strict: bool = False,
) -> bool | None:
	"""Worker-only delivery; FCM failures never affect the originating transaction."""
	if not users:
		return False if strict else None
	settings = frappe.get_single("Flutter Utils Settings")
	if not settings.get("enable_firebase_push") or not users:
		return False if strict else None
	enabled_users = frappe.get_all("User", filters={"name": ["in", users], "enabled": 1}, pluck="name")
	if not enabled_users:
		return False if strict else None
	filters: dict[str, Any] = {"user": ["in", enabled_users], "enabled": 1, "push_enabled": 1}
	if app:
		filters["app"] = app
	rows = frappe.get_all(
		REGISTRATION_DOCTYPE, filters=filters, fields=["name", "fcm_token", "push_last_seen_at"]
	)
	rows = [row for row in rows if row.fcm_token]
	if not rows:
		return False if strict else None
	try:
		from firebase_admin import messaging

		from flutter_utils.firebase import get_firebase_app

		firebase_app = get_firebase_app(settings)
		failed = False
		for offset in range(0, len(rows), 500):
			batch = rows[offset : offset + 500]
			result = messaging.send_each_for_multicast(
				messaging.MulticastMessage(
					notification=messaging.Notification(title=title, body=body),
					android=messaging.AndroidConfig(
						priority="high",
						notification=messaging.AndroidNotification(
							title=title,
							body=body,
							channel_id=ANDROID_CHANNEL_ID,
							icon=ANDROID_ICON,
							default_sound=True,
							default_vibrate_timings=True,
						),
					),
					apns=messaging.APNSConfig(
						payload=messaging.APNSPayload(
							aps=messaging.Aps(sound="default", badge=1),
						),
					),
					data={str(key): str(value) for key, value in (data or {}).items()},
					tokens=[row.fcm_token for row in batch],
				),
				app=firebase_app,
			)
			for row, response in zip(batch, result.responses, strict=True):
				if response.success:
					continue
				if isinstance(response.exception, messaging.UnregisteredError):
					frappe.db.set_value(
						REGISTRATION_DOCTYPE,
						{
							"name": row.name,
							"fcm_token": row.fcm_token,
							"push_last_seen_at": row.push_last_seen_at,
						},
						"push_enabled",
						0,
						update_modified=False,
					)
				else:
					failed = True
					frappe.log_error(
						title="Firebase Push Delivery Failed",
						message="FCM rejected a delivery. Check Firebase project configuration.",
					)
		if strict and failed:
			raise RuntimeError("PushDeliveryFailed")
		return True if strict else None
	except Exception as exc:
		frappe.log_error(
			title="Firebase Push Delivery Failed",
			message=f"FCM delivery failed ({type(exc).__name__}). Check Firebase settings and connectivity.",
		)
		if strict:
			raise RuntimeError("PushDeliveryFailed") from exc

"""Server-only control messages for the app-level Socket.IO handler."""

import hashlib

import frappe
from frappe.model.document import Document
from frappe.utils.password import get_decrypted_password

from flutter_utils.authentication.device import AUTHORIZATION_SOURCE

CONTROL_CHANNEL = "flutter_utils:realtime_control"


def disconnect_device_sockets(api_key: str, *, generation: str | None = None) -> None:
	"""Schedule a site-scoped disconnection only when the DB transaction commits."""
	if not api_key:
		return
	payload = {
		"site": frappe.local.site,
		"key_hash": hashlib.sha256(api_key.encode()).hexdigest(),
	}
	if generation:
		payload["generation"] = generation
	_queue_disconnect(payload)


def disconnect_rotated_device_sockets(credential_name: str) -> None:
	api_key = frappe.db.get_value(AUTHORIZATION_SOURCE, credential_name, "api_key")
	secret = get_decrypted_password(
		AUTHORIZATION_SOURCE, credential_name, "api_secret", raise_exception=False
	)
	if api_key and secret:
		generation = hashlib.sha256(f"{api_key}:{secret}".encode()).hexdigest()
		disconnect_device_sockets(api_key, generation=generation)


def disconnect_disabled_user_sockets(doc: Document, method: str | None = None) -> None:
	if not doc.enabled:
		_queue_disconnect({"site": frappe.local.site, "user": doc.name})


def _queue_disconnect(payload: dict[str, str]) -> None:
	# This private channel is consumed by Node, never forwarded to browser clients.
	frappe.db.after_commit.add(lambda: _publish_disconnect(payload))


def _publish_disconnect(payload: dict[str, str]) -> None:
	from frappe.utils.background_jobs import get_redis_connection_without_auth

	try:
		get_redis_connection_without_auth().publish(CONTROL_CHANNEL, frappe.as_json(payload))
	except Exception:
		# A committed revocation remains effective for HTTP/new connections. The
		# socket handler also revalidates periodically to recover missed messages.
		frappe.log_error(title="Managed device realtime disconnection failed")

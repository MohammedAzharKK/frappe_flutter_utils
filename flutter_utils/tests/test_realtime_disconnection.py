import hashlib
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe

from flutter_utils.api.realtime import get_device_context
from flutter_utils.authentication.context import AuthenticatedIdentity, clear_authenticated_identity
from flutter_utils.realtime import (
	CONTROL_CHANNEL,
	disconnect_device_sockets,
	disconnect_disabled_user_sockets,
	disconnect_rotated_device_sockets,
)


class TestRealtimeDisconnection(TestCase):
	def tearDown(self) -> None:
		clear_authenticated_identity()

	def test_disconnect_is_private_site_scoped_and_after_commit(self) -> None:
		callbacks = []
		connection = Mock()
		with (
			patch.object(frappe.db, "after_commit", Mock(add=callbacks.append)),
			patch("frappe.utils.background_jobs.get_redis_connection_without_auth", return_value=connection),
		):
			disconnect_device_sockets("device-api-key")
			connection.publish.assert_not_called()
			self.assertEqual(len(callbacks), 1)
			callbacks[0]()
			channel, raw = connection.publish.call_args.args
			self.assertEqual(channel, CONTROL_CHANNEL)
			self.assertEqual(
				frappe.parse_json(raw),
				{
					"site": frappe.local.site,
					"key_hash": hashlib.sha256(b"device-api-key").hexdigest(),
				},
			)
			self.assertNotIn("device-api-key", raw)

	def test_rotation_targets_previous_secret_generation(self) -> None:
		with (
			patch("frappe.db.get_value", return_value="key"),
			patch("flutter_utils.realtime.get_decrypted_password", return_value="old-secret"),
			patch("flutter_utils.realtime.disconnect_device_sockets") as disconnect,
		):
			disconnect_rotated_device_sockets("credential")
			disconnect.assert_called_once_with(
				"key", generation=hashlib.sha256(b"key:old-secret").hexdigest()
			)

	def test_rollback_clears_pending_disconnect_callbacks(self) -> None:
		from frappe.utils import CallbackManager

		with (
			patch.object(frappe.db, "after_commit", CallbackManager()),
			patch("frappe.utils.background_jobs.get_redis_connection_without_auth") as connection,
		):
			disconnect_device_sockets("key")
			frappe.db.rollback()
			frappe.db.after_commit.run()
			connection.assert_not_called()

	def test_low_level_revocation_schedules_disconnect_for_all_reasons(self) -> None:
		from flutter_utils.device_credentials import _revoke_credential

		for reason in ["Device Logout", "Device Limit", "Limit Reduced"]:
			with (
				self.subTest(reason=reason),
				patch("frappe.db.get_value", return_value="key"),
				patch("frappe.db.set_value") as set_value,
				patch("flutter_utils.push_notifications.deactivate_device_push"),
				patch("flutter_utils.realtime.disconnect_device_sockets") as disconnect,
			):
				_revoke_credential("credential", reason)
				self.assertEqual(set_value.call_args.args[2]["enabled"], 0)
				disconnect.assert_called_once_with("key")

	def test_admin_disable_delete_key_change_and_secret_rotation(self) -> None:
		from flutter_utils.flutter_utils.doctype.flutter_device_credential.flutter_device_credential import (
			FlutterDeviceCredential,
		)

		doc = Mock(name="credential", enabled=0, api_key="key", api_secret="new-secret")
		doc.is_new.return_value = False
		doc.get_doc_before_save.return_value = frappe._dict(api_key="old-key")
		with (
			patch("flutter_utils.push_notifications.deactivate_device_push"),
			patch("flutter_utils.realtime.disconnect_device_sockets") as disconnect,
			patch("flutter_utils.realtime.disconnect_rotated_device_sockets") as rotate,
		):
			FlutterDeviceCredential.before_save(doc)
			rotate.assert_called_once_with(doc.name)
			FlutterDeviceCredential.on_update(doc)
			self.assertEqual([call.args[0] for call in disconnect.call_args_list], ["old-key", "key"])
			disconnect.reset_mock()
			FlutterDeviceCredential.on_trash(doc)
			disconnect.assert_called_once_with("key")

	def test_disabling_user_schedules_only_disabled_user(self) -> None:
		with patch("flutter_utils.realtime._queue_disconnect") as queue:
			disconnect_disabled_user_sockets(frappe._dict(name="user", enabled=1))
			queue.assert_not_called()
			disconnect_disabled_user_sockets(frappe._dict(name="user", enabled=0))
			queue.assert_called_once_with({"site": frappe.local.site, "user": "user"})

	def test_control_publish_failure_is_logged(self) -> None:
		from flutter_utils.realtime import _publish_disconnect

		with (
			patch(
				"frappe.utils.background_jobs.get_redis_connection_without_auth", side_effect=ConnectionError
			),
			patch("frappe.log_error") as log,
		):
			_publish_disconnect({"site": "site", "user": "user"})
			log.assert_called_once()

	def test_socket_context_is_verified_and_bound_to_current_user(self) -> None:
		original_user = frappe.session.user
		try:
			frappe.set_user("device@example.invalid")
			with (
				patch("frappe.get_request_header", return_value="FlutterDevice key:secret"),
				patch(
					"flutter_utils.api.realtime.authenticate_device_credentials",
					return_value=AuthenticatedIdentity(
						user="device@example.invalid", method="managed_device", device_credential="credential"
					),
				),
			):
				self.assertEqual(
					get_device_context(), {"user": "device@example.invalid", "credential": "credential"}
				)
				frappe.set_user("other@example.invalid")
				with self.assertRaises(frappe.AuthenticationError):
					get_device_context()
		finally:
			frappe.set_user(original_user)

	def test_socket_context_rejects_other_schemes_and_revoked_credentials(self) -> None:
		for header in ["", "token key:secret", "Firebase id-token"]:
			with patch("frappe.get_request_header", return_value=header):
				with self.assertRaises(frappe.AuthenticationError):
					get_device_context()
		with (
			patch("frappe.get_request_header", return_value="FlutterDevice key:secret"),
			patch(
				"flutter_utils.api.realtime.authenticate_device_credentials",
				side_effect=frappe.AuthenticationError,
			),
		):
			with self.assertRaises(frappe.AuthenticationError):
				get_device_context()

from unittest import TestCase
from unittest.mock import patch

import frappe
from frappe.auth import validate_auth

from flutter_utils.auth import validate
from flutter_utils.authentication.context import clear_authenticated_identity


class TestRealtimeAuth(TestCase):
	def tearDown(self) -> None:
		clear_authenticated_identity()

	def test_managed_scheme_uses_auth_hook_without_source_header(self) -> None:
		original_user = frappe.session.user
		form_dict = frappe.local.form_dict
		try:
			frappe.set_user("Guest")
			with (
				patch("frappe.get_request_header", return_value="FlutterDevice key:secret"),
				patch("frappe.get_hooks", return_value=["flutter_utils.auth.validate"]),
				patch(
					"frappe.db.get_value",
					side_effect=[frappe._dict(name="credential", user="test@example.invalid"), 1],
				),
				patch("flutter_utils.authentication.device.get_decrypted_password", return_value="secret"),
				patch("frappe.auth.validate_ip_address") as validate_ip,
			):
				validate_auth()
				self.assertEqual(frappe.session.user, "test@example.invalid")
				validate_ip.assert_called_once_with("test@example.invalid")
		finally:
			frappe.set_user(original_user)
			frappe.local.form_dict = form_dict

	def test_invalid_credentials_are_rejected(self) -> None:
		for header, credential, enabled, secret in [
			("FlutterDevice", None, 1, "secret"),
			("FlutterDevice key", None, 1, "secret"),
			("FlutterDevice :secret", None, 1, "secret"),
			("FlutterDevice key:secret:extra", None, 1, "secret"),
			("FlutterDevice key:secret", None, 1, "secret"),
			("FlutterDevice key:secret", frappe._dict(name="credential", user="disabled"), 0, "secret"),
			("FlutterDevice key:wrong", frappe._dict(name="credential", user="user"), 1, "secret"),
		]:
			with (
				self.subTest(header=header, enabled=enabled),
				patch("frappe.get_request_header", return_value=header),
				patch("frappe.db.get_value", side_effect=[credential, enabled]),
				patch("flutter_utils.authentication.device.get_decrypted_password", return_value=secret),
				patch("frappe.set_user") as set_user,
			):
				with self.assertRaises(frappe.AuthenticationError):
					validate()
				set_user.assert_not_called()

	def test_revoked_or_unknown_key_requires_enabled_record(self) -> None:
		with (
			patch("frappe.get_request_header", return_value="FlutterDevice revoked:secret"),
			patch("frappe.db.get_value", return_value=None) as get_value,
		):
			with self.assertRaises(frappe.AuthenticationError):
				validate()
			self.assertEqual(get_value.call_args.args[1], {"api_key": "revoked", "enabled": 1})

	def test_standard_tokens_are_left_to_frappe(self) -> None:
		with (
			patch(
				"frappe.get_request_header",
				side_effect=lambda name, default="": (
					"token key:secret" if name == "Authorization" else default
				),
			),
			patch("frappe.db.get_value") as get_value,
		):
			validate()
			get_value.assert_not_called()

from unittest import TestCase
from unittest.mock import patch

import frappe
from frappe.auth import validate_auth

from flutter_utils.auth import validate
from flutter_utils.authentication.context import (
	AuthenticatedIdentity,
	apply_authenticated_identity,
	clear_authenticated_identity,
	get_authenticated_identity,
	require_current_device_credential,
)
from flutter_utils.authentication.device import AUTHORIZATION_SOURCE


class TestAuthenticationContext(TestCase):
	def setUp(self) -> None:
		self.original_user = frappe.session.user
		self.form_dict = frappe.local.form_dict
		clear_authenticated_identity()

	def tearDown(self) -> None:
		frappe.set_user(self.original_user)
		frappe.local.form_dict = self.form_dict
		clear_authenticated_identity()

	def _headers(self, header: str, source: str = "") -> dict[str, str]:
		return {"Authorization": header, "Frappe-Authorization-Source": source}

	def test_native_http_and_realtime_establish_same_identity(self) -> None:
		def native_auth(header: list[str]) -> None:
			if header[0].lower() == "token":
				form_dict = frappe.local.form_dict
				frappe.set_user("device@example.invalid")
				frappe.local.form_dict = form_dict

		for scheme, source in [("token", AUTHORIZATION_SOURCE), ("FlutterDevice", "")]:
			with self.subTest(scheme=scheme):
				frappe.set_user("Guest")
				form_dict = frappe._dict(example="preserved")
				frappe.local.form_dict = form_dict
				headers = self._headers(f"{scheme} key:secret", source)
				with (
					patch(
						"frappe.get_request_header",
						side_effect=lambda name, default="": headers.get(name, default),
					),
					patch("frappe.get_hooks", return_value=["flutter_utils.auth.validate"]),
					patch("frappe.auth.validate_auth_via_api_keys", side_effect=native_auth),
					patch(
						"frappe.db.get_value",
						side_effect=[
							frappe._dict(name="credential", user="device@example.invalid"),
							1,
						],
					),
					patch(
						"flutter_utils.authentication.device.get_decrypted_password", return_value="secret"
					),
					patch("frappe.auth.validate_ip_address") as validate_ip,
				):
					validate_auth()
					self.assertEqual(
						get_authenticated_identity(),
						AuthenticatedIdentity(
							user="device@example.invalid",
							method="managed_device",
							device_credential="credential",
						),
					)
					validate_ip.assert_called_once_with("device@example.invalid")
					self.assertIs(frappe.local.form_dict, form_dict)

	def test_native_http_rejects_invalid_or_disabled_credentials(self) -> None:
		for credential, enabled, secret in [
			(None, 1, "secret"),
			(frappe._dict(name="credential", user="device@example.invalid"), 0, "secret"),
			(frappe._dict(name="credential", user="device@example.invalid"), 1, "wrong"),
		]:
			with self.subTest(credential=credential, enabled=enabled, secret=secret):
				frappe.set_user("device@example.invalid")
				headers = self._headers("token key:secret", AUTHORIZATION_SOURCE)
				with (
					patch(
						"frappe.get_request_header",
						side_effect=lambda name, default="": headers.get(name, default),
					),
					patch("frappe.db.get_value", side_effect=[credential, enabled]),
					patch("flutter_utils.authentication.device.get_decrypted_password", return_value=secret),
				):
					with self.assertRaises(frappe.AuthenticationError):
						validate()
					self.assertIsNone(get_authenticated_identity())

	def test_firebase_sets_identity_and_preserves_form(self) -> None:
		form_dict = frappe.local.form_dict
		with (
			patch("frappe.get_request_header", return_value="Firebase id-token"),
			patch("flutter_utils.firebase_auth.verify_firebase_id_token") as verify,
			patch(
				"flutter_utils.firebase_auth.resolve_firebase_user",
				return_value=frappe._dict(name="firebase@example.invalid"),
			),
		):
			validate()
			verify.assert_called_once_with("id-token")
			self.assertEqual(
				get_authenticated_identity(),
				AuthenticatedIdentity(user="firebase@example.invalid", method="firebase"),
			)
			self.assertIs(frappe.local.form_dict, form_dict)

	def test_device_endpoints_reject_guest_and_malformed_tokens(self) -> None:
		headers = self._headers("token key:secret", AUTHORIZATION_SOURCE)
		frappe.set_user("Guest")
		with patch(
			"frappe.get_request_header", side_effect=lambda name, default="": headers.get(name, default)
		):
			with self.assertRaises(frappe.AuthenticationError):
				require_current_device_credential()
		frappe.set_user("device@example.invalid")
		for token in ["key", ":secret", "key:", "key:secret:extra"]:
			headers = self._headers(f"token {token}", AUTHORIZATION_SOURCE)
			with (
				self.subTest(token=token),
				patch(
					"frappe.get_request_header",
					side_effect=lambda name, default="": headers.get(name, default),
				),
			):
				with self.assertRaises(frappe.AuthenticationError):
					require_current_device_credential()

	def test_device_endpoints_reject_other_authentication_contracts(self) -> None:
		frappe.set_user("device@example.invalid")
		for header, source in [
			("", ""),
			("Firebase id-token", ""),
			("token key:secret", ""),
			("FlutterDevice key:secret", ""),
			("token key:secret", "User"),
		]:
			headers = self._headers(header, source)
			with (
				self.subTest(header=header, source=source),
				patch(
					"frappe.get_request_header",
					side_effect=lambda name, default="": headers.get(name, default),
				),
			):
				with self.assertRaises(frappe.AuthenticationError):
					require_current_device_credential()

	def test_identity_does_not_survive_user_switch_or_new_authentication(self) -> None:
		apply_authenticated_identity(AuthenticatedIdentity(user="device@example.invalid", method="firebase"))
		frappe.set_user("Guest")
		self.assertIsNone(get_authenticated_identity())
		with patch("frappe.get_request_header", return_value=""):
			validate()
		self.assertIsNone(frappe.local.flutter_utils_identity)

	def test_cached_identity_cannot_bypass_revocation(self) -> None:
		apply_authenticated_identity(
			AuthenticatedIdentity(
				user="device@example.invalid", method="managed_device", device_credential="credential"
			)
		)
		headers = self._headers("token key:secret", AUTHORIZATION_SOURCE)
		with (
			patch(
				"frappe.get_request_header", side_effect=lambda name, default="": headers.get(name, default)
			),
			patch("frappe.db.get_value", return_value=None),
		):
			with self.assertRaises(frappe.AuthenticationError):
				require_current_device_credential()

	def test_device_identity_must_match_native_authenticated_user(self) -> None:
		frappe.set_user("other@example.invalid")
		with self.assertRaises(frappe.AuthenticationError):
			apply_authenticated_identity(
				AuthenticatedIdentity(
					user="device@example.invalid", method="managed_device", device_credential="credential"
				),
				set_user=False,
			)

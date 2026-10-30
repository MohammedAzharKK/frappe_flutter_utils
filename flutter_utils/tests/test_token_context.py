from unittest import TestCase
from unittest.mock import Mock, patch

import frappe

from flutter_utils.api import auth
from flutter_utils.authentication.context import require_current_device_credential


class TestTokenContext(TestCase):
	def setUp(self) -> None:
		self.user = frappe._dict(
			name="user@example.invalid",
			full_name="Example User",
			email="user@example.invalid",
			mobile_no=None,
			user_type="System User",
		)

	def test_context_is_read_only_and_contains_no_credentials_or_csrf(self) -> None:
		with (
			patch("frappe.session", frappe._dict(user=self.user.name)),
			patch("frappe.db", Mock()) as db,
			patch("frappe.get_doc", return_value=self.user) as get_doc,
			patch("frappe.get_roles", return_value=["Organization Administrator", "All"]),
			patch("frappe.sessions.get_csrf_token") as csrf,
			patch("flutter_utils.authentication.context.require_current_device_credential") as require,
		):
			result = auth.get_token_context()

		require.assert_called_once_with()
		get_doc.assert_called_once_with("User", self.user.name)
		csrf.assert_not_called()
		self.assertEqual(db.mock_calls, [])
		self.assertEqual(
			result,
			{
				"authenticated": True,
				"auth_mode": "token",
				"user": self.user.name,
				"full_name": self.user.full_name,
				"email": self.user.email,
				"mobile_no": None,
				"user_type": "System User",
				"roles": ["All", "Organization Administrator"],
			},
		)

	def test_invalid_or_revoked_device_does_not_return_identity(self) -> None:
		with (
			patch(
				"flutter_utils.authentication.context.require_current_device_credential",
				side_effect=frappe.AuthenticationError,
			),
			patch("frappe.get_doc") as get_doc,
			self.assertRaises(frappe.AuthenticationError),
		):
			auth.get_token_context()
		get_doc.assert_not_called()

	def test_device_revalidation_rejects_revoked_disabled_and_wrong_secret(self) -> None:
		credential = frappe._dict(name="device", user=self.user.name)
		for record, enabled, secret in [
			(None, 1, "secret"),
			(credential, 0, "secret"),
			(credential, 1, "wrong"),
		]:
			with (
				self.subTest(record=record, enabled=enabled, secret=secret),
				patch("frappe.session", frappe._dict(user=self.user.name)),
				patch("flutter_utils.authentication.context.clear_authenticated_identity"),
				patch(
					"frappe.get_request_header",
					side_effect=lambda name, default="": {
						"Authorization": "token key:secret",
						"Frappe-Authorization-Source": "Flutter Device Credential",
					}.get(name, default),
				),
				patch("frappe.db", Mock(get_value=Mock(side_effect=[record, enabled]))),
				patch("flutter_utils.authentication.device.get_decrypted_password", return_value=secret),
				patch("frappe.get_doc") as get_doc,
				self.assertRaises(frappe.AuthenticationError),
			):
				auth.get_token_context()
			get_doc.assert_not_called()

	def test_guest_is_rejected(self) -> None:
		with (
			patch("frappe.session", frappe._dict(user="Guest")),
			patch("flutter_utils.authentication.context.clear_authenticated_identity"),
			self.assertRaises(frappe.AuthenticationError),
		):
			require_current_device_credential()

	def test_cookie_or_wrong_authorization_source_is_rejected(self) -> None:
		with (
			patch("frappe.session", frappe._dict(user=self.user.name)),
			patch("flutter_utils.authentication.context.clear_authenticated_identity"),
			patch("flutter_utils.authentication.headers.parse_authorization_header"),
			patch("frappe.get_request_header", return_value=""),
			patch("frappe.throw", side_effect=frappe.AuthenticationError),
			patch("flutter_utils.authentication.device.authenticate_device_credentials") as authenticate,
			self.assertRaises(frappe.AuthenticationError),
		):
			require_current_device_credential()
		authenticate.assert_not_called()

	def test_browser_session_still_returns_csrf(self) -> None:
		with (
			patch("frappe.session", frappe._dict(user=self.user.name)),
			patch("frappe.get_doc", return_value=self.user),
			patch("frappe.get_roles", return_value=["All"]),
			patch("frappe.sessions.get_csrf_token", return_value="browser-csrf") as csrf,
		):
			result = auth.get_session_context()
		self.assertEqual(result["auth_mode"], "session")
		self.assertEqual(result["csrf_token"], "browser-csrf")
		csrf.assert_called_once_with()

	def test_guest_browser_contract_is_unchanged(self) -> None:
		with patch("frappe.session", frappe._dict(user="Guest")):
			self.assertEqual(auth.get_session_context(), {"authenticated": False, "auth_mode": "session"})

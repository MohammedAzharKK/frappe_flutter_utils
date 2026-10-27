"""Isolated regression tests: no SMS providers, database writes, or site needed."""

import inspect
import json
from contextlib import ExitStack
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe

from flutter_utils.api import auth
from flutter_utils.flutter_utils.doctype.flutter_utils_settings.flutter_utils_settings import (
	FlutterUtilsSettings,
)


class TestTestPhoneLogin(TestCase):
	def setUp(self) -> None:
		self.stack = ExitStack()
		self.addCleanup(self.stack.close)
		self.settings = frappe._dict(
			enable_test_phone_login=1,
			enable_mobile_otp=1,
			test_phone_number="+12025550123",
			otp_length="6",
			modified="2026-10-26 12:00:00",
			test_mode=0,
		)
		self.settings.get_password = Mock(return_value="123456")
		self.user = frappe._dict(
			name="reviewer@example.invalid",
			full_name="Demo Reviewer",
			enabled=1,
			user_type="Website User",
		)
		self.context = {"channel": "mobile", "recipient": self.settings.test_phone_number}
		self.cache: dict[str, str] = {}
		self.stack.enter_context(patch.object(frappe.local, "form_dict", frappe._dict(), create=True))
		self.stack.enter_context(patch.object(auth, "_", side_effect=lambda text: text))
		self.stack.enter_context(patch.object(frappe, "throw", side_effect=frappe.ValidationError))
		self.stack.enter_context(patch.object(auth, "get_flutter_utils_settings", return_value=self.settings))
		self.stack.enter_context(patch.object(auth, "get_default_phone_region", return_value="IN"))
		self.stack.enter_context(patch.object(auth, "resolve_otp_context", return_value=self.context))
		self.stack.enter_context(patch.object(auth, "validate_login_target", return_value=self.user))
		self.stack.enter_context(patch.object(auth, "get_enabled_user_by_mobile", return_value=self.user))
		self.roles = self.stack.enter_context(patch.object(frappe, "get_roles", return_value=["All"]))
		self.cooldown = self.stack.enter_context(patch.object(auth, "enforce_otp_resend_cooldown"))
		self.record_cooldown = self.stack.enter_context(patch.object(auth, "record_otp_resend_cooldown"))
		self.stack.enter_context(patch.object(auth, "get_otp_ttl_seconds", return_value=300))
		self.stack.enter_context(patch.object(auth, "generate_otp", return_value="987654"))
		self.stack.enter_context(patch.object(auth, "otp_set", side_effect=self._store))
		self.stack.enter_context(patch.object(auth, "otp_get", side_effect=self.cache.get))
		self.stack.enter_context(
			patch.object(auth, "otp_delete", side_effect=lambda key: self.cache.pop(key, None))
		)
		self.deliver = self.stack.enter_context(patch.object(auth, "deliver_otp"))
		self.stack.enter_context(patch.object(auth, "hash_device_id"))
		self.stack.enter_context(patch.object(auth, "normalize_device_name"))
		self.issue = self.stack.enter_context(
			patch.object(auth, "issue_device_api_credentials", return_value={"ok": True})
		)

	def _store(self, key: str, value: str, ttl: int) -> None:
		self.assertEqual(ttl, 300)
		self.cache[key] = value

	def _send(self, purpose: str = "login") -> dict:
		return inspect.unwrap(auth.send_otp)(
			purpose=purpose, channel="mobile", mobile_no=self.context["recipient"]
		)

	def _verify(self, code: str = "123456", purpose: str = "login") -> dict:
		return inspect.unwrap(auth.verify_otp)(
			purpose=purpose,
			channel="mobile",
			mobile_no=self.context["recipient"],
			otp=code,
			device_id="demo-device",
		)

	def test_static_challenge_skips_delivery_and_hides_code_even_in_global_test_mode(self) -> None:
		for global_test_mode in (0, 1):
			with self.subTest(global_test_mode=global_test_mode):
				self.settings.test_mode = global_test_mode
				response = self._send()
				self.assertEqual(set(response), {"message"})
				payload = json.loads(next(iter(self.cache.values())))
				self.assertEqual(payload["otp"], "123456")
				self.assertIn("test_phone_login", payload)
		self.deliver.assert_not_called()
		self.cooldown.assert_called_with("login", "mobile", self.context["recipient"])
		self.record_cooldown.assert_called_with("login", "mobile", self.context["recipient"])

	def test_success_uses_existing_device_credentials_and_consumes_challenge(self) -> None:
		self._send()
		self.assertEqual(self._verify(), {"ok": True})
		self.issue.assert_called_once_with(self.user, "demo-device", None)
		with self.assertRaises(frappe.ValidationError):
			self._verify()

	def test_normal_challenges_keep_working_when_demo_configuration_changes(self) -> None:
		self.settings.enable_test_phone_login = 0
		self._send()
		self.settings.enable_test_phone_login = 1
		self.settings.modified = "2026-10-26 14:00:00"
		self.assertEqual(self._verify("987654"), {"ok": True})

	def test_cooldown_and_missing_account_fail_before_storing_or_delivering(self) -> None:
		self.cooldown.side_effect = frappe.ValidationError
		with self.assertRaises(frappe.ValidationError):
			self._send()
		self.cooldown.side_effect = None
		with patch.object(auth, "validate_login_target", side_effect=frappe.ValidationError):
			with self.assertRaises(frappe.ValidationError):
				self._send()
		self.assertFalse(self.cache)
		self.deliver.assert_not_called()
		self.record_cooldown.assert_not_called()

	def test_disabling_account_after_send_rejects_verification(self) -> None:
		self._send()
		self.user.enabled = 0
		with self.assertRaises(frappe.ValidationError):
			self._verify()
		self.issue.assert_not_called()

	def test_wrong_missing_and_expired_codes_do_not_issue_credentials(self) -> None:
		with self.assertRaises(frappe.ValidationError):
			self._verify()
		self._send()
		with self.assertRaises(frappe.ValidationError):
			self._verify("000000")
		self.cache.clear()  # Simulate Redis TTL expiry.
		with self.assertRaises(frappe.ValidationError):
			self._verify()
		self.issue.assert_not_called()

	def test_disabling_or_changing_configuration_invalidates_pending_challenges(self) -> None:
		for field, value in (
			("enable_test_phone_login", 0),
			("enable_mobile_otp", 0),
			("modified", "2026-10-26 13:00:00"),
			("test_phone_number", "+919400797246"),
		):
			with self.subTest(field=field):
				self._send()
				original = self.settings[field]
				self.settings[field] = value
				with self.assertRaises(frappe.ValidationError):
					self._verify()
				self.settings[field] = original
				self.assertFalse(self.cache)
		self.issue.assert_not_called()

	def test_rotating_static_code_invalidates_pending_challenge(self) -> None:
		self._send()
		self.settings.get_password.return_value = "654321"
		with self.assertRaises(frappe.ValidationError):
			self._verify()
		self.issue.assert_not_called()

	def test_account_reassignment_or_privilege_escalation_rejects_challenge(self) -> None:
		self._send()
		self.user.name = "different@example.invalid"
		with self.assertRaises(frappe.ValidationError):
			self._verify()
		self._send()
		self.user.user_type = "System User"
		with self.assertRaises(frappe.ValidationError):
			self._verify()
		self.issue.assert_not_called()

	def test_other_numbers_and_disabled_option_keep_normal_delivery(self) -> None:
		for field, value in (("enable_test_phone_login", 0), ("test_phone_number", "+919400797246")):
			with self.subTest(field=field):
				original = self.settings[field]
				self.settings[field] = value
				self._send()
				self.deliver.assert_called_with(
					channel="mobile",
					recipient=self.context["recipient"],
					otp="987654",
					context="login",
					full_name=self.user.full_name,
				)
				self.settings[field] = original

	def test_email_and_non_login_purposes_never_get_static_code(self) -> None:
		for purpose, channel in (
			("signup", "mobile"),
			("reset_password", "mobile"),
			("verify_mobile", "mobile"),
			("login", "email"),
		):
			with self.subTest(purpose=purpose, channel=channel):
				context = {"channel": channel, "recipient": self.context["recipient"]}
				self.assertIsNone(auth._get_test_phone_otp(self.settings, purpose, context, self.user))
		self.settings.get_password.assert_not_called()

	def test_privileged_and_disabled_accounts_are_rejected(self) -> None:
		for field, value in (
			("enabled", 0),
			("name", "Administrator"),
			("name", "Guest"),
			("user_type", "System User"),
		):
			with self.subTest(field=field, value=value):
				original = self.user[field]
				self.user[field] = value
				with self.assertRaises(frappe.ValidationError):
					self._send()
				self.user[field] = original
		for role in ("System Manager", "Super Admin"):
			self.roles.return_value = [role]
			with self.assertRaises(frappe.ValidationError):
				self._send()
		self.deliver.assert_not_called()

	def test_malformed_or_wrong_length_static_code_fails_closed(self) -> None:
		for code in ("", "1234", "abcdef", "\uff11\uff12\uff13\uff14\uff15\uff16"):
			with self.subTest(code=code):
				self.settings.get_password.return_value = code
				with self.assertRaises(frappe.ValidationError):
					self._send()
		self.deliver.assert_not_called()

	def test_public_settings_do_not_expose_demo_configuration(self) -> None:
		with patch.object(auth, "get_default_phone_region", return_value="IN"):
			result = inspect.unwrap(auth.get_otp_auth_settings)()
		self.assertEqual(set(result), {"otp_length", "otp_resend_cooldown_seconds", "otp_default_region"})

	def test_settings_validation_normalizes_number_and_rejects_bad_configuration(self) -> None:
		with patch.object(auth, "get_default_phone_region", return_value="IN"):
			self.settings.test_phone_number = "+1 202 555 0123"
			FlutterUtilsSettings._validate_test_phone_login(self.settings)
		self.assertEqual(self.settings.test_phone_number, "+12025550123")
		for field, value in (("test_phone_number", "2025550123"), ("enable_mobile_otp", 0)):
			with self.subTest(field=field):
				original = self.settings[field]
				self.settings[field] = value
				with self.assertRaises(frappe.ValidationError):
					FlutterUtilsSettings._validate_test_phone_login(self.settings)
				self.settings[field] = original
		self.settings.get_password.return_value = "1234"
		with self.assertRaises(frappe.ValidationError):
			FlutterUtilsSettings._validate_test_phone_login(self.settings)

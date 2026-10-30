"""Offline handler tests for authenticated contact-number OTP isolation."""

import inspect
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe

from flutter_utils.api import auth


class TestMobileVerificationIsolation(unittest.TestCase):
	def setUp(self) -> None:
		self.entries: dict[str, str] = {}
		self.frappe = MagicMock()
		self.frappe.session.user = "alice@example.com"
		self.frappe.AuthenticationError = frappe.AuthenticationError
		self.frappe.utils.get_fullname.return_value = "Alice"
		self.frappe.throw.side_effect = self.throw
		self.number = "+16505550100"
		patchers = [
			patch.object(auth, "frappe", self.frappe),
			patch.object(auth, "_", side_effect=lambda message: message),
			patch.object(auth, "get_flutter_utils_settings", return_value=SimpleNamespace(test_mode=True)),
			patch.object(
				auth, "resolve_otp_context", return_value={"channel": "mobile", "recipient": self.number}
			),
			patch.object(auth, "enforce_otp_resend_cooldown"),
			patch.object(auth, "record_otp_resend_cooldown"),
			patch.object(auth, "generate_otp", return_value="1234"),
			patch.object(auth, "get_otp_ttl_seconds", return_value=300),
			patch.object(auth, "build_otp_send_response", return_value={}),
			patch.object(auth, "otp_set", side_effect=self.store),
			patch.object(auth, "otp_get", side_effect=self.entries.get),
			patch.object(auth, "otp_delete", side_effect=lambda key: self.entries.pop(key, None)),
		]
		for patcher in patchers:
			patcher.start()
			self.addCleanup(patcher.stop)

	@staticmethod
	def throw(message: str, exc: type[Exception] = frappe.ValidationError) -> None:
		raise exc(message)

	def store(self, key: str, value: str, ttl: int) -> None:
		self.entries[key] = value

	def send(self) -> dict:
		return inspect.unwrap(auth.send_otp)(purpose="verify_mobile", channel="mobile", mobile_no=self.number)

	def verify(self, otp: str = "1234") -> dict:
		return inspect.unwrap(auth.verify_otp)(
			purpose="verify_mobile", channel="mobile", mobile_no=self.number, otp=otp
		)

	def test_requester_can_verify_and_challenge_is_consumed(self) -> None:
		self.send()
		self.assertEqual(self.verify(), {"verified": True, "mobile_no": self.number})
		self.assertEqual(self.entries, {})
		with self.assertRaises(frappe.ValidationError):
			self.verify()

	def test_another_user_cannot_verify_or_consume_requesters_challenge(self) -> None:
		self.send()
		challenge = self.entries.copy()
		self.frappe.session.user = "bob@example.com"
		with self.assertRaises(frappe.ValidationError):
			self.verify()
		self.assertEqual(self.entries, challenge)
		self.frappe.session.user = "alice@example.com"
		self.assertTrue(self.verify()["verified"])

	def test_same_number_challenges_do_not_overwrite_other_users(self) -> None:
		self.send()
		self.frappe.session.user = "bob@example.com"
		auth.generate_otp.return_value = "5678"
		self.send()
		self.assertEqual(len(self.entries), 2)
		self.assertTrue(self.verify("5678")["verified"])
		self.frappe.session.user = "alice@example.com"
		self.assertTrue(self.verify()["verified"])

	def test_guest_cannot_send_or_verify_or_consume_challenge(self) -> None:
		self.send()
		challenge = self.entries.copy()
		self.frappe.session.user = "Guest"
		for handler in (self.send, self.verify):
			with self.subTest(handler=handler.__name__), self.assertRaises(frappe.AuthenticationError):
				handler()
		self.assertEqual(self.entries, challenge)

	def test_incorrect_code_does_not_consume_challenge(self) -> None:
		self.send()
		with self.assertRaises(frappe.ValidationError):
			self.verify("9999")
		self.assertTrue(self.verify()["verified"])

	def test_expired_challenge_is_rejected(self) -> None:
		self.send()
		self.entries.clear()  # Simulate the OTP store's TTL expiry.
		with self.assertRaises(frappe.ValidationError):
			self.verify()

	def test_legacy_unscoped_challenge_is_not_accepted(self) -> None:
		self.entries[f"verify_mobile_otp:mobile:{self.number}"] = '{"otp": "1234"}'
		with self.assertRaises(frappe.ValidationError):
			self.verify()

	def test_other_otp_keys_and_recipient_cooldown_remain_unchanged(self) -> None:
		for user in ("alice@example.com", "bob@example.com"):
			self.frappe.session.user = user
			for purpose in ("login", "signup", "reset_password"):
				self.assertEqual(
					auth.build_otp_cache_key(purpose, "mobile", self.number),
					f"{purpose}_otp:mobile:{self.number}",
				)
			self.assertEqual(
				auth.build_otp_cooldown_key("verify_mobile", "mobile", self.number),
				f"otp_cooldown:verify_mobile:mobile:{self.number}",
			)

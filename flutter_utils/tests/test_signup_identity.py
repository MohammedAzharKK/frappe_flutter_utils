"""Rejoin-aware identity handling for Firebase sign-in and email signup."""

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from flutter_utils.api.auth import assert_signup_identity_available, create_user
from flutter_utils.firebase_auth import (
	FirebaseAuthError,
	VerifiedFirebaseIdentity,
	_get_enabled_user,
)

IDENTITY = VerifiedFirebaseIdentity(
	project_id="test-project",
	uid="google-uid",
	provider="google.com",
	email="user@example.com",
	phone_number=None,
	full_name="Test User",
)


class FirebaseReactivationTests(FrappeTestCase):
	def test_hook_may_reopen_a_self_deleted_user(self) -> None:
		user_doc = MagicMock(name="User")
		with (
			patch("flutter_utils.firebase_auth.frappe.db.exists", return_value=True),
			patch("flutter_utils.firebase_auth.frappe.db.get_value", return_value=0),
			patch(
				"flutter_utils.firebase_auth.frappe.get_hooks",
				return_value=["sync_dating.api.rejoin.reactivate_firebase_user"],
			),
			patch(
				"flutter_utils.firebase_auth.frappe.get_attr",
				return_value=lambda **kwargs: "user@example.com",
			),
			patch("flutter_utils.firebase_auth.frappe.get_doc", return_value=user_doc),
		):
			self.assertIs(_get_enabled_user("user@example.com", IDENTITY), user_doc)

	def test_no_hook_keeps_disabled_account_closed(self) -> None:
		with (
			patch("flutter_utils.firebase_auth.frappe.db.exists", return_value=True),
			patch("flutter_utils.firebase_auth.frappe.db.get_value", return_value=0),
			patch("flutter_utils.firebase_auth.frappe.get_hooks", return_value=[]),
		):
			with self.assertRaises(FirebaseAuthError) as raised:
				_get_enabled_user("user@example.com", IDENTITY)
		self.assertEqual(raised.exception.error_code, "frappe_user_disabled")

	def test_hook_returning_none_keeps_disabled_account_closed(self) -> None:
		with (
			patch("flutter_utils.firebase_auth.frappe.db.exists", return_value=True),
			patch("flutter_utils.firebase_auth.frappe.db.get_value", return_value=0),
			patch("flutter_utils.firebase_auth.frappe.get_hooks", return_value=["hooks.deny"]),
			patch("flutter_utils.firebase_auth.frappe.get_attr", return_value=lambda **kwargs: None),
		):
			with self.assertRaises(FirebaseAuthError) as raised:
				_get_enabled_user("user@example.com", IDENTITY)
		self.assertEqual(raised.exception.error_code, "frappe_user_disabled")

	def test_hook_failure_is_logged_and_keeps_account_closed(self) -> None:
		with (
			patch("flutter_utils.firebase_auth.frappe.db.exists", return_value=True),
			patch("flutter_utils.firebase_auth.frappe.db.get_value", return_value=0),
			patch("flutter_utils.firebase_auth.frappe.get_hooks", return_value=["hooks.broken"]),
			patch(
				"flutter_utils.firebase_auth.frappe.get_attr",
				return_value=MagicMock(side_effect=RuntimeError("boom")),
			),
			patch("flutter_utils.firebase_auth.frappe.log_error") as log_error,
		):
			with self.assertRaises(FirebaseAuthError):
				_get_enabled_user("user@example.com", IDENTITY)
		log_error.assert_called_once()

	def test_enabled_user_bypasses_the_hook(self) -> None:
		user_doc = MagicMock(name="User")
		with (
			patch("flutter_utils.firebase_auth.frappe.db.exists", return_value=True),
			patch("flutter_utils.firebase_auth.frappe.db.get_value", return_value=1),
			patch("flutter_utils.firebase_auth.frappe.get_hooks") as get_hooks,
			patch("flutter_utils.firebase_auth.frappe.get_doc", return_value=user_doc),
		):
			self.assertIs(_get_enabled_user("user@example.com", IDENTITY), user_doc)
		get_hooks.assert_not_called()


class SignupIdentityTests(FrappeTestCase):
	def test_deleted_email_still_owns_the_identity(self) -> None:
		with (
			patch("flutter_utils.api.auth.frappe.db.exists", return_value=True),
			patch("flutter_utils.api.auth.frappe.throw", side_effect=frappe.ValidationError),
		):
			with self.assertRaises(frappe.ValidationError):
				assert_signup_identity_available("deleted@example.com")

	def test_unused_email_is_available(self) -> None:
		with patch("flutter_utils.api.auth.frappe.db.exists", return_value=False):
			self.assertIsNone(assert_signup_identity_available("new@example.com"))

	def test_existing_mobile_is_not_available(self) -> None:
		with (
			patch("flutter_utils.api.auth.frappe.get_all", return_value=["u@sync.mobile"]),
			patch("flutter_utils.api.auth.frappe.throw", side_effect=frappe.ValidationError),
		):
			with self.assertRaises(frappe.ValidationError):
				assert_signup_identity_available("new@example.com", "+919999999999")

	def test_create_user_refuses_to_insert_a_duplicate(self) -> None:
		with (
			patch("flutter_utils.api.auth.frappe.db.exists", return_value=True),
			patch("flutter_utils.api.auth.frappe.throw", side_effect=frappe.ValidationError),
		):
			with self.assertRaises(frappe.ValidationError):
				create_user("Someone", "deleted@example.com")

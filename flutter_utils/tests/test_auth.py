from unittest import TestCase
from unittest.mock import Mock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from flutter_utils.api import auth
from flutter_utils.auth import validate


class TestFirebaseAuthHook(FrappeTestCase):
	def test_preserves_parsed_request_arguments_when_setting_user(self) -> None:
		original_user = frappe.session.user
		form_dict = frappe._dict({"answers": "serialized-answers"})
		frappe.local.form_dict = form_dict

		try:
			with (
				patch("frappe.get_request_header", return_value="Firebase valid-token"),
				patch(
					"flutter_utils.firebase_auth.verify_firebase_id_token",
					return_value={"uid": "firebase-user"},
				),
				patch(
					"flutter_utils.firebase_auth.resolve_firebase_user",
					return_value=frappe._dict(name="firebase-user@example.invalid"),
				),
			):
				validate()

			self.assertIs(frappe.local.form_dict, form_dict)
			self.assertEqual(frappe.form_dict.answers, "serialized-answers")
		finally:
			frappe.set_user(original_user)

	def test_ignores_other_authorization_schemes(self) -> None:
		with (
			patch("frappe.get_request_header", return_value="token api-key:api-secret"),
			patch("flutter_utils.firebase_auth.verify_firebase_id_token") as verify_token,
		):
			validate()

		verify_token.assert_not_called()

	def test_rejects_missing_firebase_token(self) -> None:
		with (
			patch("frappe.get_request_header", return_value="Firebase "),
			self.assertRaises(frappe.AuthenticationError),
		):
			validate()


class TestOtpCooldown(FrappeTestCase):
	def test_does_not_start_cooldown_when_login_target_is_invalid(self) -> None:
		with (
			patch(
				"flutter_utils.api.auth.get_flutter_utils_settings", return_value=frappe._dict(test_mode=True)
			),
			patch(
				"flutter_utils.api.auth.resolve_otp_context",
				return_value={"channel": "mobile", "recipient": "+919400797246"},
			),
			patch("flutter_utils.api.auth.enforce_otp_resend_cooldown"),
			patch("flutter_utils.api.auth.validate_login_target", side_effect=frappe.ValidationError()),
			patch("flutter_utils.api.auth.record_otp_resend_cooldown") as record_cooldown,
			self.assertRaises(frappe.ValidationError),
		):
			auth.send_otp(purpose="login", channel="mobile", mobile_no="+919400797246")

		record_cooldown.assert_not_called()

	def test_starts_cooldown_after_otp_is_prepared_in_test_mode(self) -> None:
		with (
			patch(
				"flutter_utils.api.auth.get_flutter_utils_settings", return_value=frappe._dict(test_mode=True)
			),
			patch(
				"flutter_utils.api.auth.resolve_otp_context",
				return_value={"channel": "mobile", "recipient": "+919400797246"},
			),
			patch("flutter_utils.api.auth.enforce_otp_resend_cooldown"),
			patch("flutter_utils.api.auth.generate_otp", return_value="1234"),
			patch(
				"flutter_utils.api.auth.validate_login_target",
				return_value=frappe._dict(full_name="Test User"),
			),
			patch("flutter_utils.api.auth.otp_set") as store_otp,
			patch("flutter_utils.api.auth.record_otp_resend_cooldown") as record_cooldown,
		):
			auth.send_otp(purpose="login", channel="mobile", mobile_no="+919400797246")

		store_otp.assert_called_once()
		record_cooldown.assert_called_once_with("login", "mobile", "+919400797246")


class TestVerifyMobileOtp(FrappeTestCase):
	def test_verify_mobile_purpose_is_supported(self) -> None:
		self.assertEqual(auth.normalize_otp_purpose("verify_mobile"), "verify_mobile")

	def test_verify_mobile_requires_a_signed_in_user(self) -> None:
		original_user = frappe.session.user
		try:
			frappe.set_user("Guest")
			with (
				patch(
					"flutter_utils.api.auth.get_flutter_utils_settings",
					return_value=frappe._dict(test_mode=True),
				),
				self.assertRaises(frappe.AuthenticationError),
			):
				auth.send_otp(
					purpose="verify_mobile",
					channel="mobile",
					mobile_no="+919400797246",
				)
		finally:
			frappe.set_user(original_user)

	def test_send_verify_mobile_uses_the_otp_store_without_signup(self) -> None:
		with (
			patch(
				"flutter_utils.api.auth.get_flutter_utils_settings",
				return_value=frappe._dict(test_mode=True),
			),
			patch(
				"flutter_utils.api.auth.resolve_otp_context",
				return_value={"channel": "mobile", "recipient": "+919400797246"},
			),
			patch("flutter_utils.api.auth.enforce_otp_resend_cooldown"),
			patch("flutter_utils.api.auth.generate_otp", return_value="1234"),
			patch("flutter_utils.api.auth.validate_signup_target") as signup_target,
			patch("flutter_utils.api.auth.otp_set") as store_otp,
			patch("flutter_utils.api.auth.record_otp_resend_cooldown"),
		):
			auth.send_otp(
				purpose="verify_mobile",
				channel="mobile",
				mobile_no="+919400797246",
			)

		signup_target.assert_not_called()
		store_otp.assert_called_once()

	def test_verify_mobile_otp_returns_verified(self) -> None:
		with (
			patch(
				"flutter_utils.api.auth.resolve_otp_context",
				return_value={"channel": "mobile", "recipient": "+919400797246"},
			),
			patch("flutter_utils.api.auth.otp_get", return_value='{"otp": "1234"}'),
			patch("flutter_utils.api.auth.otp_delete"),
		):
			result = auth.verify_otp(
				purpose="verify_mobile",
				channel="mobile",
				mobile_no="+919400797246",
				otp="1234",
			)

		self.assertEqual(
			result, {"verified": True, "mobile_no": "+919400797246"}
		)


class TestBrowserSessionAuthentication(FrappeTestCase):
	def test_guest_session_context_is_anonymous(self) -> None:
		original_user = frappe.session.user
		try:
			frappe.set_user("Guest")
			self.assertEqual(
				auth.get_session_context(),
				{"authenticated": False, "auth_mode": "session"},
			)
		finally:
			frappe.set_user(original_user)

	def test_session_mode_does_not_require_device_credentials(self) -> None:
		user = frappe._dict(name="user@example.com")
		payload = '{"otp": "1234"}'

		with (
			patch(
				"flutter_utils.api.auth.resolve_otp_context",
				return_value={"channel": "email", "recipient": "user@example.com"},
			),
			patch("flutter_utils.api.auth.otp_get", return_value=payload),
			patch("flutter_utils.api.auth.otp_delete"),
			patch("flutter_utils.api.auth.get_enabled_user_by_email", return_value=user),
			patch(
				"flutter_utils.api.auth.create_browser_session",
				return_value={"auth_mode": "session", "user": user.name},
			),
			patch("flutter_utils.api.auth.hash_device_id") as hash_device_id,
		):
			result = auth.verify_otp(
				purpose="login",
				channel="email",
				email="user@example.com",
				otp="1234",
				auth_mode="session",
			)

		self.assertEqual(result, {"auth_mode": "session", "user": "user@example.com"})
		hash_device_id.assert_not_called()

	def test_token_mode_remains_the_default(self) -> None:
		user = frappe._dict(name="user@example.com")
		payload = '{"otp": "1234"}'

		with (
			patch(
				"flutter_utils.api.auth.resolve_otp_context",
				return_value={"channel": "email", "recipient": "user@example.com"},
			),
			patch("flutter_utils.api.auth.otp_get", return_value=payload),
			patch("flutter_utils.api.auth.otp_delete"),
			patch("flutter_utils.api.auth.get_enabled_user_by_email", return_value=user),
			patch("flutter_utils.api.auth.hash_device_id"),
			patch("flutter_utils.api.auth.normalize_device_name"),
			patch(
				"flutter_utils.api.auth.issue_device_api_credentials",
				return_value={"api_key": "key", "api_secret": "secret"},
			) as issue_credentials,
		):
			result = auth.verify_otp(
				purpose="login",
				channel="email",
				email="user@example.com",
				otp="1234",
				device_id="00000000-0000-4000-8000-000000000000",
			)

		self.assertEqual(result["api_key"], "key")
		issue_credentials.assert_called_once()


class TestNativePasswordLogin(TestCase):
	def setUp(self) -> None:
		super().setUp()
		self.manager = Mock()
		self.manager.login.return_value = None
		settings = patch.object(
			auth, "get_flutter_utils_settings", return_value=frappe._dict(test_mode=False)
		)
		self.settings = settings.start().return_value
		self.addCleanup(settings.stop)
		for attribute, value in (
			("login_manager", self.manager),
			("form_dict", frappe._dict()),
			("response", frappe._dict()),
			("session", frappe._dict(user="user@example.com")),
			("request", None),
			("flags", frappe._dict()),
			("message_log", []),
			("lang", "en"),
		):
			patcher = patch.object(frappe.local, attribute, value, create=True)
			patcher.start()
			self.addCleanup(patcher.stop)

	def test_challenge_never_issues_credentials(self) -> None:
		for method in ("Email", "SMS", "OTP App"):
			for mode in ("session", "token"):
				with self.subTest(method=method, mode=mode):
					self.manager.login.return_value = False
					frappe.local.response.update(
						verification={"method": method, "setup": True}, tmp_id="challenge"
					)
					with (
						patch.object(auth, "issue_device_api_credentials") as issue,
						patch.object(auth, "build_browser_session_response") as session,
					):
						result = auth.login("user@example.com", "password", "device", auth_mode=mode)
					self.assertFalse(result["authenticated"])
					self.assertEqual(result["tmp_id"], "challenge")
					self.assertEqual(result["verification"]["method"], method)
					self.assertNotIn("otp", result)
					self.assertNotIn("otp", frappe.form_dict)
					issue.assert_not_called()
					session.assert_not_called()

	def test_success_with_and_without_otp_in_both_modes(self) -> None:
		for mode in ("session", "token"):
			for continuation in (False, True):
				with self.subTest(mode=mode, continuation=continuation):
					arguments = (
						{"otp": "012345", "tmp_id": "challenge"}
						if continuation
						else {"usr": "user@example.com", "pwd": "password"}
					)
					with (
						patch("frappe.get_doc", return_value=frappe._dict(name="user@example.com")),
						patch.object(
							auth, "issue_device_api_credentials", return_value={"api_key": "key"}
						) as issue,
						patch.object(
							auth, "build_browser_session_response", return_value={"authenticated": True}
						) as session,
					):
						result = auth.login(
							**arguments, auth_mode=mode, device_id="device" if mode == "token" else None
						)
						if mode == "session":
							self.assertTrue(result["authenticated"])
							issue.assert_not_called()
						else:
							self.assertEqual(result["api_key"], "key")
							session.assert_not_called()
						if continuation:
							self.assertEqual(frappe.form_dict.otp, "012345")
							self.assertEqual(frappe.form_dict.tmp_id, "challenge")
		self.assertEqual(self.manager.login.call_count, 4)

	def test_native_failure_never_issues_credentials(self) -> None:
		from frappe.twofactor import ExpiredLoginException

		for error in (frappe.AuthenticationError, ExpiredLoginException):
			for mode in ("session", "token"):
				with self.subTest(error=error, mode=mode):
					self.manager.login.side_effect = error("Login failed")
					with (
						patch.object(auth, "issue_device_api_credentials") as issue,
						patch.object(auth, "build_browser_session_response") as session,
						self.assertRaises(error),
					):
						auth.login(otp="012345", tmp_id="challenge", auth_mode=mode, device_id="device")
					issue.assert_not_called()
					session.assert_not_called()

	def test_incomplete_challenge_is_rejected(self) -> None:
		for arguments in ({"otp": "012345"}, {"tmp_id": "challenge"}, {}):
			with self.subTest(arguments=arguments), self.assertRaises(frappe.AuthenticationError):
				auth.login(**arguments, auth_mode="session")
		self.manager.login.assert_not_called()

	def test_missing_cached_credentials_returns_expired_challenge_code(self) -> None:
		frappe.local.response.message = "Incomplete login details"
		self.manager.login.side_effect = frappe.AuthenticationError
		with self.assertRaises(frappe.AuthenticationError) as caught:
			auth.login(otp="012345", tmp_id="expired", auth_mode="session")
		self.assertEqual(caught.exception.error_code, "login_challenge_expired")
		self.assertEqual(caught.exception.http_status_code, 401)

	def test_device_details_are_validated_before_native_login(self) -> None:
		for arguments in ({}, {"device_id": " "}, {"device_id": "device", "device_name": "x" * 141}):
			with self.subTest(arguments=arguments), self.assertRaises(frappe.ValidationError):
				auth.login("user@example.com", "password", **arguments)
		self.manager.login.assert_not_called()

	def test_legacy_password_otp_request_is_not_downgraded_to_passwordless(self) -> None:
		frappe.form_dict.password = "password"
		with (
			patch.object(auth, "get_flutter_utils_settings", return_value=frappe._dict()),
			patch.object(auth, "generate_otp") as generate,
			self.assertRaises(frappe.AuthenticationError),
		):
			auth.send_otp(purpose="login", email="user@example.com")
		generate.assert_not_called()

	def test_password_reset_redirect_does_not_issue_credentials(self) -> None:
		self.manager.login.return_value = False
		frappe.local.response.update(message="Password Reset", redirect_to="/update-password")
		with patch.object(auth, "issue_device_api_credentials") as issue:
			result = auth.login("user@example.com", "password", "device")
		self.assertFalse(result["authenticated"])
		self.assertEqual(result["message"], "Password Reset")
		issue.assert_not_called()

	def test_test_mode_only_overrides_email_and_sms_first_leg(self) -> None:
		self.settings.test_mode = True
		for method in ("Email", "SMS", "OTP App"):
			for continuation in (False, True):
				with self.subTest(method=method, continuation=continuation):
					self.manager.login.reset_mock()
					self.manager.login.return_value = False
					arguments = (
						{"otp": "012345", "tmp_id": "challenge"}
						if continuation
						else {"usr": "user@example.com", "pwd": "password"}
					)
					with (
						patch("frappe.get_system_settings", return_value=method),
						patch.object(auth, "_login_with_test_otp", return_value=False) as test_login,
					):
						auth.login(**arguments, auth_mode="session")
					if method in ("Email", "SMS") and not continuation:
						test_login.assert_called_once_with(self.manager)
						self.manager.login.assert_not_called()
					else:
						test_login.assert_not_called()
						self.manager.login.assert_called_once()

	def test_test_challenge_is_native_and_does_not_deliver_or_issue_credentials(self) -> None:
		import pyotp
		from frappe import twofactor

		self.settings.test_mode = True
		self.manager.user = "user@example.com"
		self.manager.force_user_to_reset_password.return_value = False
		secret = "JBSWY3DPEHPK3PXP"
		for method in ("Email", "SMS"):
			with (
				self.subTest(method=method),
				patch(
					"frappe.get_system_settings",
					side_effect=lambda key: method if key == "two_factor_method" else 0,
				),
				patch("frappe.clear_cache"),
				patch("frappe.generate_hash", side_effect=["first", "resend"]),
				patch.object(twofactor, "should_run_2fa", return_value=True),
				patch.object(twofactor, "get_otpsecret_for_", return_value=secret),
				patch.object(twofactor, "cache_2fa_data") as cache,
				patch.object(twofactor, "get_verification_obj") as deliver,
				patch("frappe.sendmail") as sendmail,
				patch.object(auth, "issue_device_api_credentials") as issue,
				patch.object(auth, "build_browser_session_response") as session,
			):
				for challenge in ("first", "resend"):
					result = auth.login("user@example.com", "password", auth_mode="session")
					self.assertFalse(result["authenticated"])
					self.assertEqual(result["tmp_id"], challenge)
					self.assertEqual(result["verification"]["method"], method)
					self.assertFalse(result["verification"]["token_delivery"])
					self.assertEqual(len(result["otp"]), 6)
					user, token, cached_secret, tmp_id = cache.call_args.args
					self.assertEqual((user, cached_secret, tmp_id), (self.manager.user, secret, challenge))
					self.assertTrue(pyotp.HOTP(cached_secret).verify(result["otp"], token))
				deliver.assert_not_called()
				sendmail.assert_not_called()
				issue.assert_not_called()
				session.assert_not_called()
		self.manager.post_login.assert_not_called()

	def test_test_mode_password_policy_and_invalid_password_block_challenge(self) -> None:
		from frappe import twofactor

		for disabled in (True, False):
			self.manager.authenticate.reset_mock()
			self.manager.authenticate.side_effect = frappe.AuthenticationError("Invalid password")
			with (
				self.subTest(disabled=disabled),
				patch("frappe.get_system_settings", return_value=disabled),
				patch("frappe.clear_cache"),
				patch.object(twofactor, "cache_2fa_data") as cache,
				self.assertRaises(frappe.AuthenticationError),
			):
				auth._login_with_test_otp(self.manager)
			cache.assert_not_called()
			self.assertNotIn("otp", frappe.local.response)
			if disabled:
				self.manager.authenticate.assert_not_called()
		self.manager.post_login.assert_not_called()

	def test_test_code_uses_native_cache_expiry_and_verification(self) -> None:
		import pyotp
		from frappe import twofactor

		secret = "JBSWY3DPEHPK3PXP"
		token = next(value for value in range(100) if pyotp.HOTP(secret).at(value).startswith("0"))
		values = {}
		cache = Mock()
		cache.pipeline.return_value.set.side_effect = lambda key, value, ttl: values.update({key: value})
		cache.get.side_effect = values.get
		cache.delete.side_effect = lambda key: values.pop(key, None)
		self.manager.user = "user@example.com"
		self.manager.force_user_to_reset_password.return_value = False
		self.manager.fail.side_effect = frappe.AuthenticationError("Incorrect Verification code")
		frappe.form_dict.pwd = "password"
		with (
			patch("frappe.cache", cache),
			patch(
				"frappe.get_system_settings",
				side_effect=lambda key: "Email" if key == "two_factor_method" else 0,
			),
			patch("frappe.clear_cache"),
			patch("frappe.generate_hash", return_value="challenge"),
			patch.object(twofactor, "should_run_2fa", return_value=True),
			patch.object(twofactor, "get_otpsecret_for_", return_value=secret),
			patch("pyotp.TOTP") as totp,
			patch("frappe.auth.get_login_attempt_tracker") as tracker,
		):
			totp.return_value.now.return_value = str(token)
			self.assertIs(auth._login_with_test_otp(self.manager), False)
			otp = frappe.local.response.otp
			self.assertEqual(len(otp), 6)
			self.assertTrue(otp.startswith("0"))
			cache.pipeline.return_value.set.assert_any_call("challenge_token", token, 300)
			cache.pipeline.return_value.set.assert_any_call("challenge_pwd", "password", 300)
			wrong_otp = f"{(int(otp) + 1) % 1000000:06d}"
			with self.assertRaises(frappe.AuthenticationError):
				twofactor.confirm_otp_token(self.manager, otp=wrong_otp, tmp_id="challenge")
			tracker.return_value.add_failure_attempt.assert_called_once()
			self.assertTrue(twofactor.confirm_otp_token(self.manager, otp=otp, tmp_id="challenge"))
			self.assertNotIn("challenge_token", values)
			tracker.return_value.add_success_attempt.assert_called_once()
			values.clear()
			with self.assertRaises(twofactor.ExpiredLoginException):
				twofactor.confirm_otp_token(self.manager, otp=otp, tmp_id="challenge")

	def test_test_mode_preserves_forced_password_reset(self) -> None:
		from frappe import twofactor

		self.manager.force_user_to_reset_password.return_value = True
		with (
			patch("frappe.get_system_settings", return_value=0),
			patch("frappe.clear_cache"),
			patch("frappe.get_doc") as get_doc,
			patch.object(twofactor, "cache_2fa_data") as cache,
		):
			self.assertIs(auth._login_with_test_otp(self.manager), False)
		get_doc.return_value._reset_password.assert_called_once_with(send_email=False, password_expired=True)
		self.assertEqual(frappe.local.response.message, "Password Reset")
		cache.assert_not_called()
		self.manager.post_login.assert_not_called()

	def test_test_mode_without_required_2fa_completes_native_post_login(self) -> None:
		from frappe import twofactor

		self.manager.force_user_to_reset_password.return_value = False
		frappe.form_dict.pwd = "password"
		with (
			patch("frappe.get_system_settings", return_value=0),
			patch("frappe.clear_cache"),
			patch.object(twofactor, "should_run_2fa", return_value=False),
			patch.object(twofactor, "cache_2fa_data") as cache,
		):
			self.assertIsNone(auth._login_with_test_otp(self.manager))
		self.manager.post_login.assert_called_once()
		cache.assert_not_called()
		self.assertNotIn("pwd", frappe.form_dict)
		self.assertNotIn("otp", frappe.local.response)

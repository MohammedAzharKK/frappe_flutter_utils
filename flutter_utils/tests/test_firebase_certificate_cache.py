"""Offline tests: real RSA signatures, fake Redis, no Frappe database required."""

import inspect
import json
import time
import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import requests
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from firebase_admin import auth
from google.auth import crypt, jwt

from flutter_utils import firebase_certificates as cache_module
from flutter_utils.firebase_tokens import verify_id_token


class TestFirebaseCertificateCache(unittest.TestCase):
	@classmethod
	def setUpClass(cls) -> None:
		key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
		pem = key.private_bytes(
			serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
		)
		cls.private_key = pem
		cls.signer = crypt.RSASigner.from_string(pem, key_id="test-key")
		name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Offline Firebase Test")])
		certificate = (
			x509.CertificateBuilder()
			.subject_name(name)
			.issuer_name(name)
			.public_key(key.public_key())
			.serial_number(x509.random_serial_number())
			.not_valid_before(datetime.now(UTC) - timedelta(days=1))
			.not_valid_after(datetime.now(UTC) + timedelta(days=1))
			.sign(key, hashes.SHA256())
		)
		cls.certificate = certificate.public_bytes(serialization.Encoding.PEM).decode()

	def setUp(self) -> None:
		self.entries = {}
		self.cache = MagicMock()
		self.cache.get_value.side_effect = lambda key, **kwargs: self.entries.get(key)
		self.cache.set_value.side_effect = lambda key, value, **kwargs: self.entries.update({key: value})
		self.cache.make_key.side_effect = lambda key: f"test-site:{key}"
		self.cache.lock.return_value.acquire.return_value = True
		self.response = MagicMock(status_code=200)
		self.response.headers = {"Cache-Control": "public, max-age=3600", "Age": "100"}
		self.response.json.return_value = {"test-key": self.certificate}
		self.patchers = [
			patch.object(cache_module.frappe, "cache", self.cache),
			patch.object(cache_module.frappe, "logger", return_value=MagicMock()),
			patch.object(cache_module.requests, "get", return_value=self.response),
		]
		for patcher in self.patchers:
			patcher.start()
			self.addCleanup(patcher.stop)
		self.app = SimpleNamespace(project_id="test-project")

	def token(self, **overrides) -> str:
		now = int(time.time())
		claims = {
			"aud": "test-project",
			"iss": "https://securetoken.google.com/test-project",
			"sub": "test-user",
			"iat": now - 30,
			"exp": now + 3600,
			"firebase": {"sign_in_provider": "google.com"},
		}
		claims.update(overrides)
		return jwt.encode(self.signer, claims).decode()

	def warm_cache(self, remaining: int = 3000) -> None:
		self.entries[cache_module.CACHE_KEY] = {
			"certificates": {"test-key": self.certificate},
			"expires_at": time.time() + remaining,
		}

	def test_real_signature_verified_and_shared_cache_reused(self) -> None:
		self.assertEqual(verify_id_token(self.token(), app=self.app)["uid"], "test-user")
		# A second verifier has no process-local cache; Redis alone avoids HTTP.
		self.assertEqual(verify_id_token(self.token(), app=self.app)["uid"], "test-user")
		cache_module.requests.get.assert_called_once_with(
			cache_module.CERTIFICATE_URL, timeout=(3, 5), allow_redirects=False
		)
		entry = self.entries[cache_module.CACHE_KEY]
		self.assertLessEqual(entry["expires_at"] - time.time(), 3500)
		self.assertEqual(set(entry), {"certificates", "expires_at"})

	def test_warm_login_does_not_need_google(self) -> None:
		self.warm_cache()
		cache_module.requests.get.side_effect = requests.ConnectionError("offline")
		self.assertEqual(verify_id_token(self.token(), app=self.app)["uid"], "test-user")
		cache_module.requests.get.assert_not_called()

	def test_expired_cache_and_network_failure_fail_closed(self) -> None:
		self.warm_cache(remaining=-1)
		cache_module.requests.get.side_effect = requests.ConnectionError("offline")
		with self.assertRaises(cache_module.CertificateUnavailableError):
			verify_id_token(self.token(), app=self.app)

	def test_refresh_failure_preserves_fresh_keys_without_extending_expiry(self) -> None:
		self.warm_cache(remaining=60)
		expires_at = self.entries[cache_module.CACHE_KEY]["expires_at"]
		cache_module.requests.get.side_effect = requests.Timeout("offline")
		cache_module._refresh()
		self.assertEqual(self.entries[cache_module.CACHE_KEY]["expires_at"], expires_at)

	def test_unknown_key_triggers_one_refresh_then_is_rejected(self) -> None:
		self.warm_cache()
		unknown = jwt.encode(
			crypt.RSASigner.from_string(self.private_key, key_id="unknown"),
			jwt.decode(self.token(), verify=False),
		)
		for _ in range(2):
			with self.assertRaises(auth.InvalidIdTokenError):
				verify_id_token(unknown.decode(), app=self.app)
		cache_module.requests.get.assert_called_once()

	def test_rotated_key_is_refreshed_and_verified(self) -> None:
		self.warm_cache()
		rotated = jwt.encode(
			crypt.RSASigner.from_string(self.private_key, key_id="rotated"),
			jwt.decode(self.token(), verify=False),
		)
		self.response.json.return_value = {"rotated": self.certificate}
		self.assertEqual(verify_id_token(rotated.decode(), app=self.app)["uid"], "test-user")

	def test_refresh_lock_contention_uses_fresh_cache_only(self) -> None:
		self.cache.lock.return_value.acquire.return_value = False
		with self.assertRaises(cache_module.CertificateUnavailableError):
			cache_module.get_certificates("test-key")
		cache_module.requests.get.assert_not_called()

	def test_scheduler_refreshes_near_expiry_but_not_healthy_cache(self) -> None:
		with patch.object(cache_module.frappe, "db", MagicMock()) as db:
			db.get_single_value.return_value = 1
			self.warm_cache()
			cache_module.refresh_firebase_certificates()
			cache_module.requests.get.assert_not_called()
			self.warm_cache(remaining=100)
			cache_module.refresh_firebase_certificates()
			cache_module.requests.get.assert_called_once()

	def test_scheduler_skips_sites_without_firebase_auth(self) -> None:
		with patch.object(cache_module.frappe, "db", MagicMock()) as db:
			db.get_single_value.return_value = 0
			cache_module.refresh_firebase_certificates()
		cache_module.requests.get.assert_not_called()

	def test_unusable_http_cache_lifetime_is_rejected(self) -> None:
		for control in ["public", "no-store, max-age=3600", "max-age=0", "max-age=3600, no-cache"]:
			with self.subTest(control=control):
				self.entries.clear()
				self.response.headers = {"Cache-Control": control}
				with self.assertRaises(cache_module.CertificateUnavailableError):
					cache_module.get_certificates("test-key")

	def test_malformed_certificates_are_not_cached(self) -> None:
		self.response.json.return_value = {"test-key": "not a certificate"}
		with self.assertRaises(cache_module.CertificateUnavailableError):
			cache_module.get_certificates("test-key")
		self.assertNotIn(cache_module.CACHE_KEY, self.entries)

	def test_already_aged_out_response_is_rejected(self) -> None:
		self.response.headers["Age"] = "4000"
		with self.assertRaises(cache_module.CertificateUnavailableError):
			cache_module.get_certificates("test-key")
		self.assertNotIn(cache_module.CACHE_KEY, self.entries)

	def test_redirect_is_not_accepted_or_followed(self) -> None:
		self.response.status_code = 302
		with self.assertRaises(cache_module.CertificateUnavailableError):
			cache_module.get_certificates("test-key")
		self.assertFalse(cache_module.requests.get.call_args.kwargs["allow_redirects"])

	def test_failed_cold_refresh_is_throttled(self) -> None:
		cache_module.requests.get.side_effect = requests.Timeout("offline")
		for _ in range(2):
			with self.assertRaises(cache_module.CertificateUnavailableError):
				cache_module.get_certificates("test-key")
		cache_module.requests.get.assert_called_once()

	def test_expired_token_rejected_even_with_valid_cached_certificates(self) -> None:
		self.warm_cache()
		with self.assertRaises(auth.ExpiredIdTokenError):
			verify_id_token(self.token(exp=int(time.time()) - 1), app=self.app)

	def test_wrong_project_issuer_subject_and_future_issued_at_rejected(self) -> None:
		self.warm_cache()
		for overrides in [
			{"aud": "other-project"},
			{"iss": "https://attacker.invalid"},
			{"sub": ""},
			{"sub": "a" * 129},
			{"iat": int(time.time()) + 3600},
		]:
			with self.subTest(overrides=overrides), self.assertRaises(auth.InvalidIdTokenError):
				verify_id_token(self.token(**overrides), app=self.app)

	def test_tampered_signature_rejected(self) -> None:
		self.warm_cache()
		parts = self.token().split(".")
		parts[2] = ("A" if parts[2][0] != "A" else "B") + parts[2][1:]
		with self.assertRaises(auth.InvalidIdTokenError):
			verify_id_token(".".join(parts), app=self.app)

	def test_revocation_and_disabled_user_policy_preserved(self) -> None:
		self.warm_cache()
		with patch("flutter_utils.firebase_tokens.auth.get_user") as get_user:
			get_user.return_value = SimpleNamespace(disabled=False, tokens_valid_after_timestamp=0)
			verify_id_token(self.token(), app=self.app, check_revoked=True)
			get_user.assert_called_once_with("test-user", app=self.app)
			get_user.return_value.disabled = True
			with self.assertRaises(auth.UserDisabledError):
				verify_id_token(self.token(), app=self.app, check_revoked=True)
			get_user.return_value.disabled = False
			get_user.return_value.tokens_valid_after_timestamp = int(time.time()) * 1000
			with self.assertRaises(auth.RevokedIdTokenError):
				verify_id_token(self.token(), app=self.app, check_revoked=True)

	def test_missing_kid_and_wrong_algorithm_rejected_before_network(self) -> None:
		claims = jwt.decode(self.token(), verify=False)
		for token in [
			jwt.encode(crypt.RSASigner.from_string(self.private_key), claims),
			jwt.encode(self.signer, claims, header={"alg": "HS256"}),
		]:
			with self.assertRaises(auth.InvalidIdTokenError):
				verify_id_token(token.decode(), app=self.app)
		cache_module.requests.get.assert_not_called()

	def test_revocation_check_not_performed_when_disabled(self) -> None:
		self.warm_cache()
		with patch("flutter_utils.firebase_tokens.auth.get_user") as get_user:
			verify_id_token(self.token(), app=self.app)
		get_user.assert_not_called()

	def test_public_error_contract_for_outage_and_expired_token(self) -> None:
		from flutter_utils.firebase_auth import FirebaseAuthError, verify_firebase_id_token

		settings = SimpleNamespace(firebase_project_id="test-project", firebase_check_revoked_tokens=False)
		with (
			patch("flutter_utils.firebase_auth._get_enabled_firebase_settings", return_value=settings),
			patch("flutter_utils.firebase_auth.get_firebase_app", return_value=self.app),
			patch("flutter_utils.firebase_auth.frappe.log_error") as log_error,
		):
			cache_module.requests.get.side_effect = requests.ConnectionError("untrusted remote detail")
			with self.assertRaises(FirebaseAuthError) as context:
				verify_firebase_id_token(self.token())
			self.assertEqual(context.exception.error_code, "firebase_unavailable")
			self.assertEqual(context.exception.http_status_code, 503)
			self.assertNotIn("untrusted remote detail", str(log_error.call_args))
			self.warm_cache()
			with self.assertRaises(FirebaseAuthError) as context:
				verify_firebase_id_token(self.token(exp=int(time.time()) - 1))
			self.assertEqual(context.exception.error_code, "firebase_token_expired")


class TestFirebaseLoginTimings(unittest.TestCase):
	def test_token_exchange_contract_and_secret_free_stage_timings(self) -> None:
		from flutter_utils.api.auth import firebase_token_login

		identity = SimpleNamespace(provider="google.com")
		with (
			patch("flutter_utils.firebase_auth.verify_firebase_id_token", return_value=identity),
			patch("flutter_utils.firebase_auth.resolve_firebase_user", return_value=SimpleNamespace()),
			patch(
				"flutter_utils.api.auth.issue_device_api_credentials",
				return_value={
					"api_key": "sensitive-key",
					"api_secret": "sensitive-secret",
					"authorization_source": "Flutter Device Credential",
				},
			),
			patch("frappe.logger", return_value=MagicMock()) as logger,
		):
			# Exercise the handler offline, not Frappe's request/rate-limit middleware.
			response = inspect.unwrap(firebase_token_login)("sensitive-token", "installation-uuid")
		self.assertEqual(response["auth_mode"], "token")
		self.assertEqual(response["provider"], "google.com")
		self.assertEqual(response["api_secret"], "sensitive-secret")
		records = [json.loads(call.args[0]) for call in logger.return_value.info.call_args_list]
		self.assertEqual(
			[record["stage"] for record in records],
			[
				"verify_identity",
				"resolve_user",
				"issue_credentials",
				"login_total",
			],
		)
		self.assertEqual(len({record["attempt_id"] for record in records}), 1)
		self.assertTrue(all(record["result"] == "ok" for record in records))
		self.assertNotIn("sensitive", str(records))

	def test_failed_exchange_is_timed_without_logging_exception_payload(self) -> None:
		from flutter_utils.api.auth import firebase_token_login

		with (
			patch(
				"flutter_utils.firebase_auth.verify_firebase_id_token",
				side_effect=RuntimeError("sensitive-token"),
			),
			patch("flutter_utils.api.auth.issue_device_api_credentials") as issue,
			patch("frappe.logger", return_value=MagicMock()) as logger,
			self.assertRaises(RuntimeError),
		):
			inspect.unwrap(firebase_token_login)("sensitive-token", "installation-uuid")
		issue.assert_not_called()
		records = [json.loads(call.args[0]) for call in logger.return_value.info.call_args_list]
		self.assertEqual([record["stage"] for record in records], ["verify_identity", "login_total"])
		self.assertTrue(all(record["result"] == "failed" for record in records))
		self.assertNotIn("sensitive", str(records))

import time
from unittest.mock import MagicMock, patch

import frappe
from firebase_admin import auth as firebase_admin_auth
from frappe.tests.utils import FrappeTestCase

from flutter_utils.api.auth import (
	firebase_session_login,
	firebase_token_login,
)
from flutter_utils.api.auth import (
	link_firebase_identities as link_firebase_identities_endpoint,
)
from flutter_utils.firebase_auth import (
	FirebaseAuthError,
	VerifiedFirebaseIdentity,
	get_verified_user_contacts,
	link_firebase_identities,
	parse_service_account_json,
	resolve_firebase_user,
	verify_firebase_id_token,
)


class TestFirebaseTokenVerification(FrappeTestCase):
	def setUp(self) -> None:
		self.settings = frappe._dict(
			{
				"enable_firebase_auth": 1,
				"enable_firebase_phone_auth": 1,
				"enable_firebase_google_auth": 1,
				"firebase_check_revoked_tokens": 0,
				"firebase_project_id": "test-project",
			}
		)

	def test_verifies_google_identity(self) -> None:
		firebase_app = MagicMock()
		claims = {
			"aud": "test-project",
			"uid": "google-uid",
			"email": "USER@example.com",
			"email_verified": True,
			"name": "Test User",
			"firebase": {"sign_in_provider": "google.com"},
		}
		with (
			patch("flutter_utils.firebase_auth._get_enabled_firebase_settings", return_value=self.settings),
			patch("flutter_utils.firebase_auth.get_firebase_app", return_value=firebase_app),
			patch("flutter_utils.firebase_auth.auth.verify_id_token", return_value=claims) as verify_token,
		):
			identity = verify_firebase_id_token("firebase-token")

		self.assertEqual(identity.email, "user@example.com")
		self.assertEqual(identity.provider, "google.com")
		verify_token.assert_called_once_with(
			"firebase-token",
			app=firebase_app,
			check_revoked=False,
		)

	def test_checks_revocation_when_explicitly_enabled(self) -> None:
		self.settings.firebase_check_revoked_tokens = 1
		firebase_app = MagicMock()
		claims = {
			"aud": "test-project",
			"uid": "google-uid",
			"email": "user@example.com",
			"email_verified": True,
			"firebase": {"sign_in_provider": "google.com"},
		}
		with (
			patch("flutter_utils.firebase_auth._get_enabled_firebase_settings", return_value=self.settings),
			patch("flutter_utils.firebase_auth.get_firebase_app", return_value=firebase_app),
			patch("flutter_utils.firebase_auth.auth.verify_id_token", return_value=claims) as verify_token,
		):
			verify_firebase_id_token("firebase-token")

		verify_token.assert_called_once_with("firebase-token", app=firebase_app, check_revoked=True)

	def test_rejects_disabled_phone_provider(self) -> None:
		self.settings.enable_firebase_phone_auth = 0
		claims = {
			"aud": "test-project",
			"uid": "phone-uid",
			"phone_number": "+919744714697",
			"firebase": {"sign_in_provider": "phone"},
		}
		with (
			patch("flutter_utils.firebase_auth._get_enabled_firebase_settings", return_value=self.settings),
			patch("flutter_utils.firebase_auth.get_firebase_app", return_value=MagicMock()),
			patch("flutter_utils.firebase_auth.auth.verify_id_token", return_value=claims),
			self.assertRaises(FirebaseAuthError) as context,
		):
			verify_firebase_id_token("firebase-token")

		self.assertEqual(context.exception.error_code, "firebase_phone_disabled")
		self.assertEqual(context.exception.http_status_code, 403)

	def test_maps_expired_token_error(self) -> None:
		with (
			patch("flutter_utils.firebase_auth._get_enabled_firebase_settings", return_value=self.settings),
			patch("flutter_utils.firebase_auth.get_firebase_app", return_value=MagicMock()),
			patch(
				"flutter_utils.firebase_auth.auth.verify_id_token",
				side_effect=firebase_admin_auth.ExpiredIdTokenError("expired", None),
			),
			self.assertRaises(FirebaseAuthError) as context,
		):
			verify_firebase_id_token("firebase-token")

		self.assertEqual(context.exception.error_code, "firebase_token_expired")

	def test_service_account_parser_rejects_non_service_credentials(self) -> None:
		with self.assertRaises(frappe.ValidationError):
			parse_service_account_json('{"type": "authorized_user"}')


class TestVerifiedFirebaseContacts(FrappeTestCase):
	def setUp(self) -> None:
		frappe.set_user("Administrator")
		self.user_name = "firebase-contact-test@example.invalid"
		self.project_id = "firebase-contact-test-project"
		self._delete_identities()
		frappe.delete_doc_if_exists("User", self.user_name, force=True)
		frappe.get_doc(
			{
				"doctype": "User",
				"email": self.user_name,
				"first_name": "Firebase Contact Test",
				"user_type": "Website User",
				"enabled": 1,
				"send_welcome_email": 0,
			}
		).insert(ignore_permissions=True)

	def tearDown(self) -> None:
		frappe.set_user("Administrator")
		self._delete_identities()
		frappe.delete_doc_if_exists("User", self.user_name, force=True)
		frappe.db.commit()

	def test_returns_verified_contacts(self) -> None:
		self._insert_identity("verified@example.com", "+919876543210")

		email, phone_number = get_verified_user_contacts(self.user_name)

		self.assertEqual(email, "verified@example.com")
		self.assertEqual(phone_number, "+919876543210")

	def test_hides_placeholder_email(self) -> None:
		self._insert_identity("firebase-user@users.invalid", None)

		email, phone_number = get_verified_user_contacts(self.user_name)

		self.assertIsNone(email)
		self.assertIsNone(phone_number)

	def _insert_identity(self, email: str | None, phone_number: str | None) -> None:
		frappe.get_doc(
			{
				"doctype": "Firebase Auth Identity",
				"firebase_project_id": self.project_id,
				"firebase_uid": "firebase-contact-test-uid",
				"user": self.user_name,
				"last_sign_in_provider": "google.com",
				"email": email,
				"phone_number": phone_number,
			}
		).insert(ignore_permissions=True)

	def _delete_identities(self) -> None:
		for identity_name in frappe.get_all(
			"Firebase Auth Identity", filters={"user": self.user_name}, pluck="name"
		):
			frappe.delete_doc("Firebase Auth Identity", identity_name, force=True)


class TestFirebaseUserProvisioning(FrappeTestCase):
	def setUp(self) -> None:
		frappe.set_user("Administrator")
		self.settings_patch = patch(
			"flutter_utils.firebase_auth._get_enabled_firebase_settings",
			return_value=frappe._dict({"firebase_auto_create_users": 1}),
		)
		self.settings_patch.start()
		self.created_users: list[str] = []

	def tearDown(self) -> None:
		self.settings_patch.stop()
		for user in self.created_users:
			for mapping in frappe.get_all("Firebase Auth Identity", filters={"user": user}, pluck="name"):
				frappe.delete_doc("Firebase Auth Identity", mapping, force=True)
			frappe.delete_doc_if_exists("User", user, force=True)

	def test_auto_creates_google_website_user_and_reuses_mapping(self) -> None:
		email = "firebase-google-test@example.com"
		frappe.delete_doc_if_exists("User", email, force=True)
		identity = VerifiedFirebaseIdentity(
			project_id="test-project",
			uid="google-user-test",
			provider="google.com",
			email=email,
			phone_number=None,
			full_name="Firebase Google Test",
		)

		user = resolve_firebase_user(identity)
		self.created_users.append(user.name)
		resolved_again = resolve_firebase_user(identity)

		self.assertEqual(user.name, email)
		self.assertEqual(user.user_type, "Website User")
		self.assertEqual(resolved_again.name, user.name)
		self.assertEqual(
			frappe.db.get_value("Firebase Auth Identity", {"firebase_uid": identity.uid}, "user"),
			user.name,
		)

	def test_auto_creates_phone_user_with_internal_email(self) -> None:
		identity = VerifiedFirebaseIdentity(
			project_id="test-project",
			uid="phone-user-test",
			provider="phone",
			email=None,
			phone_number="+16505550101",
			full_name=None,
		)

		user = resolve_firebase_user(identity)
		self.created_users.append(user.name)

		self.assertTrue(user.name.startswith("firebase-"))
		self.assertTrue(user.name.endswith("@users.invalid"))
		self.assertEqual(user.mobile_no, identity.phone_number)

	def test_linking_phone_identity_updates_stale_user_mobile_number(self) -> None:
		google_identity = VerifiedFirebaseIdentity(
			project_id="test-project",
			uid="link-google-user-test",
			provider="google.com",
			email="firebase-link-test@example.com",
			phone_number=None,
			full_name="Firebase Link Test",
		)
		phone_identity = VerifiedFirebaseIdentity(
			project_id="test-project",
			uid="link-phone-user-test",
			provider="phone",
			email=None,
			phone_number="+16505550102",
			full_name=None,
		)

		user = resolve_firebase_user(google_identity)
		self.created_users.append(user.name)
		# Creating the identity mapping runs the app's after_insert hook, which may
		# update the User; reload before mutating so the timestamp check passes.
		user.reload()
		user.mobile_no = "+16505550100"
		user.save(ignore_permissions=True)

		user = link_firebase_identities(google_identity, phone_identity)
		linked_again = link_firebase_identities(google_identity, phone_identity)
		mappings = frappe.get_all(
			"Firebase Auth Identity",
			filters={"user": user.name, "firebase_project_id": "test-project"},
			pluck="firebase_uid",
		)

		self.assertEqual(linked_again.name, user.name)
		self.assertCountEqual(mappings, [google_identity.uid, phone_identity.uid])
		self.assertEqual(frappe.db.get_value("User", user.name, "mobile_no"), phone_identity.phone_number)

	def test_rejects_linking_identities_mapped_to_different_users(self) -> None:
		google_identity = VerifiedFirebaseIdentity(
			project_id="test-project",
			uid="conflict-google-user-test",
			provider="google.com",
			email="firebase-conflict-test@example.com",
			phone_number=None,
			full_name="Firebase Conflict Test",
		)
		phone_identity = VerifiedFirebaseIdentity(
			project_id="test-project",
			uid="conflict-phone-user-test",
			provider="phone",
			email=None,
			phone_number="+16505550103",
			full_name=None,
		)
		google_user = resolve_firebase_user(google_identity)
		phone_user = resolve_firebase_user(phone_identity)
		self.created_users.extend([google_user.name, phone_user.name])

		with self.assertRaises(FirebaseAuthError) as context:
			link_firebase_identities(google_identity, phone_identity)

		self.assertEqual(context.exception.error_code, "firebase_accounts_require_manual_merge")
		self.assertEqual(context.exception.http_status_code, 409)

	def test_rejects_linking_identities_from_different_projects(self) -> None:
		primary_identity = VerifiedFirebaseIdentity(
			project_id="first-project",
			uid="first-project-user",
			provider="google.com",
			email="first-project@example.com",
			phone_number=None,
			full_name=None,
		)
		secondary_identity = VerifiedFirebaseIdentity(
			project_id="second-project",
			uid="second-project-user",
			provider="phone",
			email=None,
			phone_number="+919744714600",
			full_name=None,
		)

		with self.assertRaises(FirebaseAuthError) as context:
			link_firebase_identities(primary_identity, secondary_identity)

		self.assertEqual(context.exception.error_code, "firebase_project_mismatch")


class TestFirebaseLoginEndpoints(FrappeTestCase):
	def setUp(self) -> None:
		self.identity = VerifiedFirebaseIdentity(
			project_id="test-project",
			uid="endpoint-user",
			provider="google.com",
			email="endpoint@example.com",
			phone_number=None,
			full_name="Endpoint User",
			authenticated_at=int(time.time()),
		)
		self.user = frappe._dict(
			{
				"name": "endpoint@example.com",
				"full_name": "Endpoint User",
				"email": "endpoint@example.com",
				"mobile_no": None,
			}
		)

	def test_session_login_does_not_return_api_credentials(self) -> None:
		login_manager = MagicMock()
		with (
			patch("flutter_utils.firebase_auth.verify_firebase_id_token", return_value=self.identity),
			patch("flutter_utils.firebase_auth.resolve_firebase_user", return_value=self.user),
			patch("frappe.auth.LoginManager", return_value=login_manager),
		):
			response = firebase_session_login("firebase-token")

		login_manager.login_as.assert_called_once_with(self.user.name)
		self.assertEqual(response["auth_mode"], "session")
		self.assertNotIn("api_key", response)
		self.assertNotIn("api_secret", response)

	def test_token_login_uses_device_api_credential_contract(self) -> None:
		with (
			patch("flutter_utils.firebase_auth.verify_firebase_id_token", return_value=self.identity),
			patch("flutter_utils.firebase_auth.resolve_firebase_user", return_value=self.user),
			patch(
				"flutter_utils.api.auth.issue_device_api_credentials",
				return_value={
					"api_key": "key",
					"api_secret": "secret",
					"authorization_source": "Flutter Device Credential",
				},
			),
		):
			response = firebase_token_login("firebase-token", "device-id", "Test Device")

		self.assertEqual(response["auth_mode"], "token")
		self.assertEqual(response["api_key"], "key")
		self.assertEqual(response["authorization_source"], "Flutter Device Credential")
		self.assertEqual(response["provider"], "google.com")

	def test_token_login_rejects_missing_device_id_before_verifying_token(self) -> None:
		with (
			patch("flutter_utils.firebase_auth.verify_firebase_id_token") as verify_token,
			self.assertRaises(frappe.ValidationError),
		):
			firebase_token_login("firebase-token")

		verify_token.assert_not_called()

	def test_link_endpoint_verifies_both_tokens_and_returns_uids(self) -> None:
		secondary_identity = VerifiedFirebaseIdentity(
			project_id="test-project",
			uid="secondary-endpoint-user",
			provider="phone",
			email=None,
			phone_number="+919744714601",
			full_name=None,
			authenticated_at=int(time.time()),
		)
		with (
			patch(
				"flutter_utils.firebase_auth.verify_firebase_id_token",
				side_effect=[self.identity, secondary_identity],
			) as verify_token,
			patch(
				"flutter_utils.firebase_auth.link_firebase_identities",
				return_value=self.user,
			) as link_identities,
		):
			response = link_firebase_identities_endpoint("primary-token", "secondary-token")

		self.assertEqual(verify_token.call_args_list[0].args, ("primary-token",))
		self.assertEqual(verify_token.call_args_list[1].args, ("secondary-token",))
		link_identities.assert_called_once_with(self.identity, secondary_identity)
		self.assertEqual(response["user"], self.user.name)
		self.assertEqual(
			response["firebase_uids"],
			[self.identity.uid, secondary_identity.uid],
		)

	def test_link_endpoint_requires_recent_authentication(self) -> None:
		stale_identity = VerifiedFirebaseIdentity(
			project_id="test-project",
			uid="stale-endpoint-user",
			provider="google.com",
			email="stale@example.com",
			phone_number=None,
			full_name=None,
			authenticated_at=int(time.time()) - 301,
		)
		with (
			patch(
				"flutter_utils.firebase_auth.verify_firebase_id_token",
				side_effect=[stale_identity, self.identity],
			),
			patch("flutter_utils.firebase_auth.link_firebase_identities") as link_identities,
			self.assertRaises(FirebaseAuthError) as context,
		):
			link_firebase_identities_endpoint("stale-token", "secondary-token")

		self.assertEqual(context.exception.error_code, "firebase_recent_login_required")
		link_identities.assert_not_called()

"""Native ORM/hook tests. Run on a migrated, disposable Frappe test site."""

from __future__ import annotations

import base64
import io
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe
from frappe.model.document import Document

from flutter_utils.api import verification as api
from flutter_utils.verification import service
from flutter_utils.verification.setup import seed_defaults


class TestVerificationIntegration(TestCase):
	def setUp(self) -> None:
		if not getattr(frappe.local, "site", None) or not getattr(frappe.local, "db", None):
			self.skipTest("Requires a migrated disposable Frappe test site.")
		self.assertTrue(frappe.db.exists("DocType", "Verification Document"), "Migrate the test site first.")
		self.old_user = frappe.session.user
		self.old_login_manager = getattr(frappe.local, "login_manager", None)
		self.transaction = service.atomic()
		self.transaction.__enter__()
		self.addCleanup(self._rollback)
		frappe.set_user("Administrator")
		frappe.local.login_manager = Mock(user="Administrator")
		seed_defaults()
		self.subject = self._user("Applicant")
		self.other = self._user("Other")
		self.reviewer = self._user("Reviewer", reviewer=True)
		frappe.set_user(self.subject)

	def _rollback(self) -> None:
		frappe.set_user(self.old_user)
		frappe.local.login_manager = self.old_login_manager
		self.transaction.__exit__(RuntimeError, RuntimeError("Test rollback"), None)

	def _user(self, label: str, reviewer: bool = False) -> str:
		doc = frappe.get_doc(
			{
				"doctype": "User",
				"email": f"verification-{frappe.generate_hash(length=12)}@example.invalid",
				"first_name": label,
				"enabled": 1,
				"send_welcome_email": 0,
				"roles": [{"role": "Verification Reviewer"}] if reviewer else [],
			}
		).insert(ignore_permissions=True)
		return doc.name

	def _create(self, request: str | None = None) -> Document:
		# A genuine small PNG, not a fake MIME header. Native File validation remains enabled.
		content = base64.b64decode(
			"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="
		)
		return service.save_record(
			"Verification Document",
			{
				"title": "Identity",
				"document_type": "National ID",
				"source_app": "flutter_utils",
				"reference_doctype": "User",
				"reference_name": self.subject,
				"verification_purpose": "KYC",
				"verification_request": request,
			},
			idempotency_key=frappe.generate_hash(),
			uploads=[
				(
					SimpleNamespace(filename="identity.png", stream=io.BytesIO(content)),
					"Front",
				)
			],
		)

	def test_native_insert_children_and_safe_api_list(self) -> None:
		doc = self._create()
		loaded = frappe.get_doc(doc.doctype, doc.name)
		self.assertEqual(len(loaded.files), 1)
		self.assertEqual(loaded.files[0].revision, 1)
		payload = api.get_document(doc.name)
		self.assertTrue(payload["files"][0]["available"])
		self.assertNotIn("/private/files/", str(payload))
		self.assertIn(
			doc.name, [item["id"] for item in api.get_documents(reference_name=self.subject)["items"]]
		)

	def test_native_direct_saves_cannot_bypass_service(self) -> None:
		doc = self._create()
		doc.approval_status = "Approved"
		with self.assertRaises(frappe.PermissionError):
			doc.save(ignore_permissions=True)
		with self.assertRaises(frappe.PermissionError):
			doc.db_set("approval_status", "Approved")
		with self.assertRaises(frappe.PermissionError):
			frappe.delete_doc(doc.doctype, doc.name, ignore_permissions=True)

	def test_native_file_permission_and_mutation_guards(self) -> None:
		doc = self._create()
		file = frappe.get_doc("File", doc.primary_file)
		self.assertTrue(frappe.has_permission("File", doc=file, user=self.subject))
		self.assertFalse(frappe.has_permission("File", doc=file, user=self.other))
		frappe.set_user(self.other)
		self.assertFalse(file.is_downloadable())
		frappe.set_user(self.subject)
		self.assertTrue(file.is_downloadable())
		file.is_private = 0
		with self.assertRaises(frappe.PermissionError):
			file.save(ignore_permissions=True)
		with self.assertRaises(frappe.PermissionError):
			frappe.delete_doc("File", file.name, ignore_permissions=True)
		self.assertTrue(frappe.get_doc("File", file.name).get_content())

	def test_native_review_and_evidence_revision(self) -> None:
		doc = self._create()
		frappe.set_user(self.reviewer)
		approved = service.transition(doc.doctype, doc.name, "Approved", 1, review_notes="Internal")
		self.assertEqual(approved.approval_status, "Approved")
		self.assertEqual(approved.document_revision, 2)
		self.assertEqual(len(approved.files), 2)
		frappe.set_user(self.subject)
		self.assertNotIn("review_notes", api.get_document(doc.name))
		with self.assertRaises(frappe.TimestampMismatchError):
			service.save_record(doc.doctype, {"title": "Changed"}, doc.name, 1)

	def test_native_request_readiness_and_invalidation(self) -> None:
		request = service.save_record(
			"Verification Request",
			{
				"title": "KYC request",
				"source_app": "flutter_utils",
				"reference_doctype": "User",
				"reference_name": self.subject,
				"verification_purpose": "KYC",
			},
			idempotency_key=frappe.generate_hash(),
		)
		doc = self._create(request.name)
		frappe.set_user(self.reviewer)
		doc = service.transition(doc.doctype, doc.name, "Approved", 1)
		self.assertEqual(service.request_requirements(request), {})
		service.transition(request.doctype, request.name, "Approved", 1)
		service.transition(doc.doctype, doc.name, "Revoked", doc.document_revision, "Renew evidence")
		self.assertEqual(frappe.db.get_value(request.doctype, request.name, "approval_status"), "Pending")

	def test_native_in_app_outbox_does_not_send_implicit_email(self) -> None:
		from flutter_utils.verification.notifications import deliver

		doc = self._create()
		event = frappe.db.get_value("Verification Review Log", {"record_name": doc.name}, "name")
		name = frappe.db.get_value(
			"Verification Notification Delivery",
			{"event": event, "recipient": self.subject, "channel": "In App"},
			"name",
		)
		with patch("frappe.desk.doctype.notification_log.notification_log.send_notification_email") as send:
			deliver(name)
			deliver(name)
			send.assert_not_called()
		self.assertEqual(frappe.db.count("Notification Log", {"name": f"verification-{name}"}), 1)

	def test_native_desk_list_does_not_expose_self_review_notes(self) -> None:
		frappe.set_user(self.reviewer)
		doc = service.save_record(
			"Verification Document",
			{
				"title": "Own profile",
				"document_type": "National ID",
				"source_app": "flutter_utils",
				"reference_doctype": "User",
				"reference_name": self.reviewer,
				"verification_purpose": "Profile",
			},
			idempotency_key=frappe.generate_hash(),
		)
		rows = frappe.get_list(
			"Verification Document", filters={"name": doc.name}, fields=["name", "review_notes"]
		)
		self.assertEqual(rows, [])

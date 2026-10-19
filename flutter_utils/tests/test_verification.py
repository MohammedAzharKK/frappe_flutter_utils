"""Database-independent service/contract tests; run with Python unittest.

The in-memory ORM exercises real policies, controllers, and services. Native ORM,
hook, and file integration is covered separately by test_verification_integration.
"""

from __future__ import annotations

import copy
import inspect
import io
import logging
from contextlib import ExitStack
from datetime import datetime
from types import SimpleNamespace
from typing import Any
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe
from frappe.utils import CallbackManager

from flutter_utils.api import verification as api
from flutter_utils.verification import evidence, model, notifications, permissions, policies, service, tasks
from flutter_utils.verification import file as verification_file
from flutter_utils.verification.notification_log import (
	VerificationNotificationMixin,
	verification_notification,
)


def throw(message: str, exc: type[Exception] = frappe.ValidationError, **kwargs: Any) -> None:
	raise exc(message)


class MemoryDoc(frappe._dict):
	def __init__(self, data: dict[str, Any], store: "MemoryDB", new: bool = True):
		super().__init__(copy.deepcopy(data))
		self._store = store
		self._new = new
		self.setdefault("files", [])
		self.files = [frappe._dict(row) for row in self.files]

	def is_new(self) -> bool:
		return self._new

	def set(self, key: str, value: Any) -> None:
		self[key] = value

	def append(self, field: str, value: dict[str, Any]) -> None:
		self.setdefault(field, []).append(frappe._dict(name=f"child-{len(self.get(field, []))}", **value))

	def _validate_document(self, policy: Any) -> None:
		model.VerificationRecord._validate_document(self, policy)

	def get_doc_before_save(self) -> Any:
		return self._store.docs.get((self.doctype, self.name))

	def insert(self, ignore_permissions: bool = False, set_name: str | None = None) -> "MemoryDoc":
		self.name = set_name or self.get("name") or f"memory-{len(self._store.docs)}"
		if self.doctype == "File":
			self.file_url = f"/private/files/{self.file_name}"
		self._persist()
		self._new = False
		return self

	def save(self, ignore_permissions: bool = False) -> "MemoryDoc":
		self._persist()
		return self

	def _persist(self) -> None:
		if self.doctype in model.RECORD_DOCTYPES:
			model.VerificationRecord.validate(self)
		elif self.doctype in {"Verification Review Log", "Verification Notification Delivery"}:
			model.ImmutableVerificationRecord.validate(self)
		elif self.doctype == "File":
			permissions.protect_file(self)
		if (self.doctype, self.name) in self._store.docs and self.is_new():
			raise frappe.DuplicateEntryError
		self.setdefault("creation", len(self._store.docs))
		self._store.docs[(self.doctype, self.name)] = frappe._dict(
			copy.deepcopy({key: value for key, value in self.items() if not key.startswith("_")})
		)


class MemoryDB:
	def __init__(self) -> None:
		self.docs: dict[tuple[str, str], Any] = {}
		self.savepoints: dict[str, Any] = {}
		self.locks: list[tuple[str, str]] = []
		for field in ("before_commit", "after_commit", "before_rollback", "after_rollback"):
			setattr(self, field, CallbackManager())

	def savepoint(self, name: str) -> None:
		self.savepoints[name] = copy.deepcopy(self.docs)

	def rollback(self, save_point: str) -> None:
		self.docs = self.savepoints[save_point]

	def exists(self, doctype: str, name: Any) -> bool:
		if doctype == "DocType":
			return True
		return bool(self.get_value(doctype, name))

	def escape(self, value: str) -> str:
		return "'" + value.replace("'", "''") + "'"

	def get_value(
		self, doctype: str, filters: Any, field: str = "name", for_update: bool = False, as_dict: bool = False
	) -> Any:
		if for_update:
			self.locks.append((doctype, filters))
		rows = self.get_all(doctype, filters=filters if isinstance(filters, dict) else {"name": filters})
		if as_dict:
			return rows[0] if rows else None
		return rows[0].get(field) if rows else None

	def get_all(self, doctype: str, **kwargs: Any) -> list[Any]:
		rows = [copy.deepcopy(doc) for (kind, _), doc in self.docs.items() if kind == doctype]
		if doctype == "Verification Document File":
			rows = [
				frappe._dict(**row, parent=name, parenttype=kind, parentfield="files", idx=index)
				for (kind, name), doc in self.docs.items()
				if kind == "Verification Document"
				for index, row in enumerate(doc.files)
			]
		filters = kwargs.get("filters", {})

		def match(row: Any) -> bool:
			for key, value in filters.items():
				actual = row.get(key)
				if isinstance(value, (list, tuple)):
					op, target = value
					if op == "in" and actual not in target:
						return False
					if op == "<" and not actual < target:
						return False
					if op == ">" and not actual > target:
						return False
					if op == "is" and bool(actual) != (target == "set"):
						return False
				elif actual != value and not (value == 0 and actual is None):
					return False
			return True

		rows = [row for row in rows if match(row)]
		start = kwargs.get("limit_start", 0)
		rows = rows[start : start + kwargs.get("limit_page_length", len(rows))]
		if kwargs.get("pluck"):
			return [row[kwargs["pluck"]] for row in rows]
		fields = kwargs.get("fields")
		if isinstance(fields, list):
			return [frappe._dict({field: row.get(field) for field in fields}) for row in rows]
		return rows

	def get_doc(self, doctype: str | dict[str, Any], name: str | None = None) -> MemoryDoc:
		if isinstance(doctype, dict):
			return MemoryDoc(doctype, self, new=not bool(doctype.get("name")))
		if (doctype, name) not in self.docs:
			raise frappe.DoesNotExistError
		return MemoryDoc(self.docs[(doctype, name)], self, new=False)


class CustomSubjectPolicy(policies.VerificationPolicy):
	reference_doctypes = ("Test Organization",)
	purposes = ("Registration",)

	def can(self, doc: Any, action: str, user: str) -> bool:
		return user == "alice" and action in {"create", "edit", "read", "archive", "withdraw"}

	def document_types(self, doc: Any) -> list[str]:
		return ["National ID"]

	def tenant(self, doc: Any) -> tuple[str, str]:
		return "Test Organization", doc.reference_name


class TestVerification(TestCase):
	def setUp(self) -> None:
		self.stack = ExitStack()
		self.addCleanup(self.stack.close)
		self.db = MemoryDB()
		self.real_counts = service.usable_document_counts
		self.session = frappe._dict(user="alice")
		self.local = SimpleNamespace(_realtime_log=[])
		patches = [
			patch.object(service, "usable_document_counts", side_effect=self._counts),
			patch.object(
				api,
				"current_rows",
				side_effect=lambda docs: [
					row
					for doc in docs
					for row in self.db.get_all(
						"Verification Document File",
						filters={"parent": doc.name, "revision": doc.document_revision},
					)
				],
			),
			patch("frappe.db", self.db),
			patch("frappe.session", self.session),
			patch("frappe.local", self.local),
			patch("frappe.request", None),
			patch("frappe.throw", throw),
			patch("frappe.get_doc", self.db.get_doc),
			patch("frappe.get_all", self.db.get_all),
			patch(
				"frappe.get_roles",
				lambda user=None: (
					["Verification Reviewer"] if (user or self.session.user) == "reviewer" else []
				),
			),
			patch(
				"frappe.get_hooks",
				lambda key: {"custom": ["custom.Policy"]} if key == "verification_policies" else {},
			),
			patch("frappe.get_attr", return_value=CustomSubjectPolicy),
			patch("frappe.generate_hash", side_effect=lambda length=12: "x" * length),
			patch("frappe.publish_realtime"),
			patch("frappe.enqueue"),
			patch("frappe.log_error"),
			patch("frappe.sendmail"),
			patch("frappe.get_meta", return_value=Mock(has_field=lambda field: False)),
			patch("frappe.utils.get_url", lambda path: f"https://test.invalid{path}"),
			patch.object(
				evidence, "get_signed_params", lambda params: f"file_id={params['file_id']}&signature=test"
			),
			patch.object(evidence, "get_max_file_size", return_value=10 * 1024 * 1024),
			patch.object(service, "get_max_file_size", return_value=10 * 1024 * 1024),
			patch.object(model, "today", return_value="2026-10-07"),
		]
		for module in (api, model, policies, evidence, service, verification_file):
			patches.append(patch.object(module, "_", lambda text: text))
		for module in (service, notifications, evidence):
			patches.append(patch.object(module, "now_datetime", return_value=datetime(2026, 10, 7, 12)))
		for item in patches:
			self.stack.enter_context(item)
		for user in ("alice", "reviewer", "other"):
			self.db.docs[("User", user)] = frappe._dict(
				doctype="User", name=user, email=f"{user}@example.invalid", enabled=1
			)
		self.db.docs[("Test Organization", "org-1")] = frappe._dict(doctype="Test Organization", name="org-1")
		self.db.docs[("Verification Document Type", "National ID")] = frappe._dict(
			doctype="Verification Document Type",
			name="National ID",
			enabled=1,
			required_fields="",
			allowed_extensions="pdf\npng",
			allowed_roles="Front\nBack",
			max_files=2,
			max_file_size_mb=1,
			warning_days=30,
			requires_expiry=0,
		)

	def create(self, request: str | None = None, uploads: bool = True, **fields: Any) -> Any:
		data = {
			"title": "Identity",
			"document_type": "National ID",
			"source_app": "flutter_utils",
			"reference_doctype": "User",
			"reference_name": "alice",
			"verification_purpose": "KYC",
			**fields,
		}
		if request:
			data["verification_request"] = request
		return service.save_record(
			"Verification Document",
			data,
			idempotency_key=f"key-{len(self.db.docs)}",
			uploads=self.uploads() if uploads else None,
		)

	def _counts(self, requests: list[str]) -> dict[str, dict[str, int]]:
		result: dict[str, dict[str, int]] = {}
		for row in self.db.get_all("Verification Document", filters={"approval_status": "Approved"}):
			file = self.db.docs.get(("File", row.primary_file))
			if (
				row.verification_request in requests
				and not row.is_archived
				and not row.is_withdrawn
				and file
				and file.is_private
				and file.attached_to_name == row.name
				and model.validity(row.expiry_date) != "Expired"
			):
				counts = result.setdefault(row.verification_request, {})
				counts[row.document_type] = counts.get(row.document_type, 0) + 1
		return result

	def uploads(self, content: bytes = b"%PDF-1.4 test", role: str = "Front") -> list[Any]:
		return [(SimpleNamespace(filename="identity.pdf", stream=io.BytesIO(content)), role)]

	def request(self) -> Any:
		return service.save_record(
			"Verification Request",
			{
				"title": "KYC",
				"source_app": "flutter_utils",
				"reference_doctype": "User",
				"reference_name": "alice",
				"verification_purpose": "KYC",
			},
			idempotency_key="request",
		)

	def review(self, doc: Any, action: str = "Approved", reason: str | None = None) -> Any:
		self.session.user = "reviewer"
		result = service.transition(doc.doctype, doc.name, action, doc.document_revision, reason)
		self.session.user = "alice"
		return result

	def test_personal_submission_private_files_and_one_event(self) -> None:
		doc = self.create()
		self.assertEqual(doc.approval_status, "Pending")
		self.assertEqual(doc.contact_user, "alice")
		self.assertEqual(len(self.db.get_all("Verification Review Log")), 1)
		file = self.db.get_doc("File", doc.primary_file)
		self.assertEqual(file.is_private, 1)
		self.assertEqual(file.attached_to_name, doc.name)
		result = service.serialize(doc)
		self.assertNotIn("/private/files/", str(result))
		self.assertIn("download_evidence", result["files"][0]["url"])

	def test_custom_subject_needs_no_user_contact_and_resolves_tenant(self) -> None:
		doc = self.create(
			source_app="custom",
			reference_doctype="Test Organization",
			reference_name="org-1",
			verification_purpose="Registration",
		)
		self.assertIsNone(doc.contact_user)
		self.assertEqual((doc.tenant_doctype, doc.tenant_name), ("Test Organization", "org-1"))

	def test_subject_tenant_change_denies_old_scope_and_skips_delivery(self) -> None:
		doc = self.create(
			source_app="custom",
			reference_doctype="Test Organization",
			reference_name="org-1",
			verification_purpose="Registration",
		)
		delivery = self.db.get_all("Verification Notification Delivery")[0]
		with patch.object(CustomSubjectPolicy, "tenant", return_value=("Test Organization", "new-tenant")):
			with self.assertRaises(frappe.PermissionError):
				service.serialize(doc)
			self.assertFalse(
				permissions.file_permission(self.db.get_doc("File", doc.primary_file), "read", "alice")
			)
			notifications.deliver(delivery.name)
		self.assertEqual(self.db.get_doc(delivery.doctype, delivery.name).status, "Skipped")

	def test_unregistered_integration_and_subject_rejected(self) -> None:
		for fields in (
			{"source_app": "unknown"},
			{"reference_doctype": "Company"},
			{"verification_purpose": "Unknown"},
		):
			with self.assertRaises((frappe.PermissionError, frappe.ValidationError)):
				self.create(**fields)

	def test_other_users_and_guests_cannot_read_or_create(self) -> None:
		doc = self.create()
		for user in ("other", "Guest"):
			self.session.user = user
			with self.assertRaises(frappe.PermissionError):
				service.serialize(doc)
			with self.assertRaises(frappe.PermissionError):
				self.create()

	def test_creation_retries_return_same_record_without_duplicate_event(self) -> None:
		doc = self.create()
		key = "retry"
		data = {field: doc.get(field) for field in (*service.CONTEXT_FIELDS, "title", "document_type")}
		first = service.save_record(doc.doctype, data, idempotency_key=key)
		second = service.save_record(doc.doctype, data, idempotency_key=key)
		self.assertEqual(first.name, second.name)
		self.assertEqual(len(self.db.get_all("Verification Review Log")), 2)

	def test_server_fields_and_missing_idempotency_key_rejected(self) -> None:
		for fields in ({"approval_status": "Approved"}, {"tenant_name": "other"}, {"contact_user": "other"}):
			with self.assertRaises(frappe.ValidationError):
				self.create(**fields)
		with self.assertRaises(frappe.ValidationError):
			service.save_record("Verification Request", {})

	def test_self_approval_and_review_without_role_denied(self) -> None:
		doc = self.create()
		with self.assertRaises(frappe.PermissionError):
			service.transition(doc.doctype, doc.name, "Approved", 1)
		self.session.user = "other"
		with self.assertRaises(frappe.PermissionError):
			service.transition(doc.doctype, doc.name, "Approved", 1)

	def test_review_transitions_and_mandatory_reasons(self) -> None:
		doc = self.create()
		self.session.user = "reviewer"
		with self.assertRaises(frappe.ValidationError):
			service.transition(doc.doctype, doc.name, "Rejected", 1, " ")
		approved = service.transition(doc.doctype, doc.name, "Approved", 1, review_notes="Internal only")
		with self.assertRaises(frappe.ValidationError):
			service.transition(doc.doctype, doc.name, "Approved", approved.document_revision)
		with self.assertRaises(frappe.ValidationError):
			service.transition(doc.doctype, doc.name, "Revoked", approved.document_revision)
		revoked = service.transition(
			doc.doctype, doc.name, "Revoked", approved.document_revision, "Renew evidence"
		)
		self.assertEqual(revoked.approval_status, "Pending")

	def test_approved_evidence_replacement_resets_and_retains_history(self) -> None:
		doc = self.review(self.create())
		old_file = doc.primary_file
		updated = service.save_record(
			doc.doctype, {}, doc.name, doc.document_revision, uploads=self.uploads(b"%PDF-new")
		)
		self.assertEqual(updated.approval_status, "Pending")
		self.assertIsNone(updated.reviewed_by)
		self.assertNotEqual(updated.primary_file, old_file)
		self.assertIn(old_file, [row.file for row in updated.files])

	def test_critical_metadata_change_invalidates_but_cosmetic_change_does_not(self) -> None:
		doc = self.review(self.create())
		doc = service.save_record(
			doc.doctype, {"description": "Updated note"}, doc.name, doc.document_revision
		)
		self.assertEqual(doc.approval_status, "Approved")
		doc = service.save_record(doc.doctype, {"document_number": "123"}, doc.name, doc.document_revision)
		self.assertEqual(doc.approval_status, "Pending")

	def test_rejected_replacement_resubmits(self) -> None:
		doc = self.review(self.create(), "Rejected", "Blurry evidence")
		doc = service.save_record(doc.doctype, {}, doc.name, doc.document_revision, uploads=self.uploads())
		self.assertEqual(doc.approval_status, "Pending")
		self.assertIsNone(doc.rejection_reason)
		self.assertEqual(doc.submission_count, 2)
		self.assertEqual(self.db.get_all("Verification Review Log")[-1].action, "Resubmitted")

	def test_stale_revision_rejected_and_parent_lock_precedes_document(self) -> None:
		request = self.request()
		doc = self.create(request.name)
		self.review(doc)
		with self.assertRaises(frappe.TimestampMismatchError):
			service.save_record(doc.doctype, {"title": "Changed"}, doc.name, 1)
		self.assertEqual(
			self.db.locks[-2:], [("Verification Request", request.name), (doc.doctype, doc.name)]
		)

	def test_request_requires_approved_evidence_and_revocation_invalidates_request(self) -> None:
		request = self.request()
		with self.assertRaises(frappe.ValidationError):
			self.review(request)
		self.session.user = "alice"
		doc = self.review(self.create(request.name))
		request = self.review(request)
		self.assertEqual(request.approval_status, "Approved")
		self.review(doc, "Revoked", "License withdrawn")
		self.assertEqual(self.db.get_doc(request.doctype, request.name).approval_status, "Pending")

	def test_request_subject_mismatch_rolls_back(self) -> None:
		request = self.request()
		count = len(self.db.docs)
		with self.assertRaises(frappe.ValidationError):
			self.create(request.name, verification_purpose="Profile")
		self.assertEqual(len(self.db.docs), count)

	def test_expired_and_missing_evidence_cannot_be_approved(self) -> None:
		for doc in (self.create(uploads=False), self.create(expiry_date="2026-10-06")):
			with self.assertRaises(frappe.ValidationError):
				self.review(doc)
			self.session.user = "alice"

	def test_expiry_and_no_expiry_validation(self) -> None:
		for fields in (
			{"issue_date": "2027-01-01", "expiry_date": "2026-01-01"},
			{"no_expiry": 1, "expiry_date": "2027-01-01"},
		):
			with self.assertRaises(frappe.ValidationError):
				self.create(**fields)
		self.assertEqual(model.validity("2026-10-06"), "Expired")
		self.assertEqual(model.validity("2026-10-07"), "Expiring Soon")
		self.assertEqual(model.validity("2027-01-01"), "Active")

	def test_invalid_uploads_rollback_document_and_outbox(self) -> None:
		for content, role in (
			(b"<html>bad</html>", "Front"),
			(b"%PDF-test", "Unknown"),
			(b"%PDF-" + b"x" * (1024 * 1024), "Front"),
		):
			count = len(self.db.docs)
			with self.assertRaises(frappe.ValidationError):
				service.save_record(
					"Verification Document",
					{
						"title": "Identity",
						"document_type": "National ID",
						"source_app": "flutter_utils",
						"reference_doctype": "User",
						"reference_name": "alice",
						"verification_purpose": "KYC",
					},
					idempotency_key="bad",
					uploads=self.uploads(content, role),
				)
			self.assertEqual(len(self.db.docs), count)

	def test_direct_record_and_audit_mutations_denied(self) -> None:
		doc = self.create()
		with self.assertRaises(frappe.PermissionError):
			doc.save(ignore_permissions=True)
		with self.assertRaises(frappe.PermissionError):
			model.VerificationRecord.db_set(doc, "approval_status", "Approved")
		event = self.db.get_doc("Verification Review Log", self.db.get_all("Verification Review Log")[0].name)
		with self.assertRaises(frappe.PermissionError), model.internal_write():
			event.save(ignore_permissions=True)

	def test_file_owner_does_not_bypass_subject_and_file_mutation_blocked(self) -> None:
		doc = self.create()
		file = self.db.get_doc("File", doc.primary_file)
		self.assertFalse(permissions.file_permission(file, "read", "other"))
		self.assertTrue(permissions.file_permission(file, "read", "alice"))
		self.assertFalse(permissions.file_permission(file, "write", "alice"))
		with self.assertRaises(frappe.PermissionError):
			permissions.protect_file(file)

	def test_native_download_mixin_checks_subject_and_leaves_other_files_unchanged(self) -> None:
		from flutter_utils.verification.file import VerificationFileMixin

		class Original:
			def is_downloadable(self) -> bool:
				return True

		class File(VerificationFileMixin, Original):
			attached_to_doctype = "Verification Document"
			attached_to_name = "record"
			is_private = 1

		with patch("flutter_utils.verification.file.file_permission", return_value=False):
			self.assertFalse(File().is_downloadable())
		file = File()
		file.attached_to_doctype = "Other Document"
		self.assertTrue(file.is_downloadable())

	def test_file_mixin_blocks_blob_side_effects_before_core_methods(self) -> None:
		from flutter_utils.verification.file import VerificationFileMixin

		core_insert, core_delete = Mock(), Mock()

		class Original:
			before_insert = core_insert
			on_trash = core_delete

		class File(VerificationFileMixin, Original):
			attached_to_doctype = "Verification Document"

		with self.assertRaises(frappe.PermissionError):
			File().on_trash()
		core_delete.assert_not_called()
		with patch("flutter_utils.verification.file.protect_file", side_effect=frappe.PermissionError):
			with self.assertRaises(frappe.PermissionError):
				File().before_insert()
		core_insert.assert_not_called()

	def test_missing_file_never_returns_private_path(self) -> None:
		doc = self.create()
		result = service.serialize(doc, {})
		self.assertFalse(result["files"][0]["available"])
		self.assertIsNone(result["files"][0]["url"])

	def test_signing_failure_returns_unavailable_without_raw_fallback(self) -> None:
		doc = self.create()
		with patch.object(evidence, "get_signed_params", side_effect=RuntimeError("private path")):
			result = service.serialize(doc)
		self.assertFalse(result["files"][0]["available"])
		self.assertIsNone(result["files"][0]["url"])
		self.assertNotIn("private path", str(frappe.log_error.call_args))

	def test_confidential_notes_not_returned_to_applicant(self) -> None:
		doc = self.create()
		self.session.user = "reviewer"
		doc = service.transition(doc.doctype, doc.name, "Approved", 1, review_notes="Internal fraud concern")
		self.assertEqual(service.serialize(doc)["review_notes"], "Internal fraud concern")
		self.session.user = "alice"
		self.assertNotIn("review_notes", service.serialize(doc))
		self.assertFalse(permissions.has_permission(doc, "read", "alice"))
		self.assertNotIn("review_notes", api.get_review_history(doc.doctype, doc.name)[0])
		self.session.user = "reviewer"
		approved_events = [
			event for event in api.get_review_history(doc.doctype, doc.name) if event.action == "Approved"
		]
		self.assertEqual(approved_events[0].review_notes, "Internal fraud concern")

	def test_archive_preserves_evidence_and_blocks_mutations(self) -> None:
		doc = self.create()
		doc = service.deactivate(doc.doctype, doc.name, "Archived", 1)
		self.assertTrue(doc.is_archived)
		self.assertTrue(doc.files)
		self.assertEqual(service.allowed_actions(doc), [])
		with self.assertRaises(frappe.ValidationError):
			service.save_record(doc.doctype, {"title": "Changed"}, doc.name, doc.document_revision)
		with self.assertRaises(frappe.PermissionError):
			model.VerificationRecord.on_trash(doc)

	def test_notifications_are_specific_escaped_and_after_commit(self) -> None:
		self.create(title="<script>bad</script>")
		delivery = self.db.get_all("Verification Notification Delivery")[0]
		self.assertTrue(delivery.subject)
		self.assertIn("&lt;script&gt;", delivery.message)
		self.assertIn("awaiting review", delivery.message)
		self.assertEqual(delivery.recipient, "alice")
		frappe.enqueue.assert_not_called()
		self.db.after_commit.run()
		frappe.enqueue.assert_called_once()
		self.assertEqual(len(self.db.get_all("Verification Review Log")), 1)

	def test_in_app_delivery_retries_do_not_duplicate_notification(self) -> None:
		self.create()
		delivery = self.db.get_all("Verification Notification Delivery")[0]
		notifications.deliver(delivery.name)
		notifications.deliver(delivery.name)
		self.assertEqual(len(self.db.get_all("Notification Log")), 1)
		self.assertEqual(self.db.get_doc(delivery.doctype, delivery.name).status, "Delivered")
		frappe.sendmail.assert_not_called()

	def test_delivery_rechecks_permission_and_records_retryable_failure(self) -> None:
		self.create()
		delivery = self.db.get_all("Verification Notification Delivery")[0]
		with patch.object(notifications, "_create_in_app", side_effect=RuntimeError("private details")):
			notifications.deliver(delivery.name)
		failed = self.db.get_doc(delivery.doctype, delivery.name)
		self.assertEqual(failed.status, "Failed")
		self.assertEqual(failed.last_error, "RuntimeError")
		self.assertNotIn("private details", str(frappe.log_error.call_args))
		self.db.docs[("User", "alice")].enabled = 0
		notifications.deliver(delivery.name)
		self.assertEqual(self.db.get_doc(delivery.doctype, delivery.name).status, "Skipped")

	def test_no_recipients_is_recorded_without_user_association(self) -> None:
		with patch.object(CustomSubjectPolicy, "recipients", return_value=[]):
			self.create(
				source_app="custom",
				reference_doctype="Test Organization",
				reference_name="org-1",
				verification_purpose="Registration",
			)
		self.assertEqual(
			self.db.get_all("Verification Review Log")[0].notification_state, "No eligible recipients"
		)
		self.assertEqual(self.db.get_all("Verification Notification Delivery"), [])

	def test_assignment_validates_reviewer_and_notifies(self) -> None:
		doc = self.create()
		self.session.user = "reviewer"
		with self.assertRaises(frappe.ValidationError):
			service.assign_reviewer(doc.doctype, doc.name, "other", 1)
		doc = service.assign_reviewer(doc.doctype, doc.name, "reviewer", 1)
		self.assertEqual(doc.assigned_reviewer, "reviewer")
		self.assertEqual(len(self.db.get_all("Verification Notification Delivery")), 3)

	def test_expiry_refresh_emits_once_and_keeps_historical_approval(self) -> None:
		doc = self.review(self.create(expiry_date="2026-10-08"))
		with patch.object(model, "today", return_value="2026-10-09"):
			tasks._refresh_one(doc.name, {"National ID": 30})
			tasks._refresh_one(doc.name, {"National ID": 30})
		current = self.db.get_doc(doc.doctype, doc.name)
		self.assertEqual(current.approval_status, "Approved")
		self.assertEqual(current.validity_status, "Expired")
		self.assertEqual(len(self.db.get_all("Verification Review Log", filters={"action": "Expired"})), 1)

	def test_atomic_rollback_discards_realtime_and_new_commit_callbacks(self) -> None:
		callback = Mock()
		with self.assertRaises(ValueError), service.atomic():
			self.local._realtime_log.append(("secret", {}))
			self.db.after_commit.add(callback)
			raise ValueError
		self.db.after_commit.run()
		callback.assert_not_called()
		self.assertFalse(getattr(self.local, "_realtime_log", []))

	def test_api_read_list_history_and_guest_denial(self) -> None:
		doc = self.create()
		self.assertEqual(api.get_document(doc.name)["id"], doc.name)
		self.assertEqual(len(api.get_documents()["items"]), 1)
		self.assertEqual(api.get_review_history(doc.doctype, doc.name)[0].action, "Submitted")
		self.assertNotIn("actor", api.get_review_history(doc.doctype, doc.name)[0])
		self.session.user = "other"
		self.assertEqual(api.get_documents()["items"], [])
		self.session.user = "Guest"
		for action in (api.get_documents, api.get_upload_config, lambda: api.get_document(doc.name)):
			with self.assertRaises(frappe.AuthenticationError):
				action()

	def test_unknown_state_action_rejected(self) -> None:
		with self.assertRaises(frappe.ValidationError):
			service.check_transition("Delete", "Pending", None)

	def test_real_query_builders_compile_bounded_children_and_grouped_readiness(self) -> None:
		from frappe.query_builder.builder import MariaDB

		sql = Mock(return_value=[])
		self.local.db = SimpleNamespace(sql=sql)
		self.local.flags = frappe._dict(in_safe_exec=False)
		with (
			patch("frappe.qb", MariaDB),
			patch.object(service, "today", return_value="2026-10-07"),
			patch("frappe.logger", return_value=logging.getLogger("verification-tests")),
		):
			self.assertEqual(self.real_counts(["request-1", "request-2"]), {})
			self.assertEqual(
				evidence.current_rows(
					[
						frappe._dict(name="document-1", document_revision=2),
						frappe._dict(name="document-2", document_revision=5),
					]
				),
				[],
			)
		self.assertIn("JOIN", sql.call_args_list[0].args[0])
		self.assertIn("GROUP BY", sql.call_args_list[0].args[0])
		self.assertIn("revision", sql.call_args_list[1].args[0])
		self.assertRegex(sql.call_args_list[1].args[0], r"revision`?\s*=\s*5")

	def test_all_public_api_routes_reject_guest(self) -> None:
		self.session.user = "Guest"
		for name, function in inspect.getmembers(api, inspect.isfunction):
			if name.startswith("_") or function.__module__ != api.__name__:
				continue
			# download_evidence is a signed-link endpoint: a guest reaches it only
			# through the HMAC signature, which is asserted separately.
			if name == "download_evidence":
				continue
			args = {
				param: ("Verification Document" if param == "record_doctype" else "placeholder")
				for param, info in inspect.signature(function).parameters.items()
				if info.default is inspect.Parameter.empty
			}
			with self.subTest(endpoint=name), self.assertRaises(frappe.AuthenticationError):
				function(**args)

	def test_list_scope_does_not_reveal_other_users_rows(self) -> None:
		self.create()
		self.session.user = "other"
		self.assertEqual(api.get_documents(page_size=1), {"items": [], "next_offset": None})
		with self.assertRaises(frappe.PermissionError):
			api.get_documents(source_app="custom")

	def test_api_document_write_routes_share_state_machine(self) -> None:
		data = {
			"title": "Identity",
			"document_type": "National ID",
			"source_app": "flutter_utils",
			"reference_doctype": "User",
			"reference_name": "alice",
			"verification_purpose": "KYC",
		}
		request = SimpleNamespace(files=SimpleNamespace(getlist=lambda key: [self.uploads()[0][0]]))
		with patch("frappe.request", request):
			doc = api.save_document(data, idempotency_key="api-create", evidence_roles=["Front"])
		name = doc["id"]
		self.session.user = "reviewer"
		doc = api.approve_document(name, 1)
		doc = api.revoke_document(name, doc["document_revision"], "Replacement needed")
		doc = api.reject_document(name, doc["document_revision"], "Please correct")
		doc = api.reset_document(name, doc["document_revision"])
		doc = api.reject_document(name, doc["document_revision"], "Please resubmit")
		self.session.user = "alice"
		doc = api.resubmit_document(name, doc["document_revision"])
		self.assertEqual(doc["approval_status"], "Pending")
		self.assertTrue(api.get_evidence_history(name))
		self.assertTrue(api.archive_document(name, doc["document_revision"])["is_archived"])
		other = self.create()
		self.assertTrue(api.withdraw_document(other.name, other.document_revision)["is_withdrawn"])

	def test_api_request_routes_and_assignment(self) -> None:
		request = api.save_request(
			{
				"title": "KYC",
				"source_app": "flutter_utils",
				"reference_doctype": "User",
				"reference_name": "alice",
				"verification_purpose": "KYC",
			},
			idempotency_key="request-api",
		)
		doc = self.review(self.create(request["id"]))
		self.session.user = "reviewer"
		assigned = api.assign_reviewer("Verification Request", request["id"], "reviewer", 1)
		request = api.review_request(request["id"], "Approved", assigned["document_revision"])
		self.assertEqual(request["approval_status"], "Approved")
		self.session.user = "alice"
		self.assertEqual(api.get_request(request["id"])["id"], request["id"])
		self.assertTrue(api.get_requests()["items"])
		self.assertTrue(api.archive_request(request["id"], request["document_revision"])["is_archived"])
		self.assertEqual(doc.approval_status, "Approved")

	def test_api_configuration_and_upload_roles_validation(self) -> None:
		self.assertEqual(len(api.get_document_types("flutter_utils", "User", "alice", "KYC")), 1)
		with patch.object(api, "get_max_file_size", return_value=1024):
			self.assertEqual(api.get_upload_config()["max_file_size_bytes"], 1024)
		request = SimpleNamespace(files=SimpleNamespace(getlist=lambda key: [self.uploads()[0][0]]))
		with patch("frappe.request", request), self.assertRaises(frappe.ValidationError):
			api._uploads([])
		with self.assertRaises(frappe.ValidationError):
			api._data("[]")

	def _downloadable_file(self) -> Any:
		doc = self.create()
		file = self.db.get_doc("File", doc.primary_file)
		file.get_content = lambda: b"private evidence"
		self.local.response = frappe._dict()
		return file

	def _serve_from_file(self, file: Any) -> Any:
		return patch(
			"frappe.get_doc",
			side_effect=lambda kind, name=None: file if kind == "File" else self.db.get_doc(kind, name),
		)

	def test_signature_helper_accepts_only_a_valid_signature(self) -> None:
		with patch.object(api, "_sign_message", side_effect=lambda params: f"sig:{params}"):
			self.local.flags = frappe._dict()

			def request(method: str, query: bytes) -> Any:
				return SimpleNamespace(method=method, query_string=query, form={}, data=None)

			with patch(
				"frappe.request", request("GET", b"file_id=abc&_signature=sig:file_id=abc")
			):
				self.assertTrue(api._has_valid_signature())
			with patch("frappe.request", request("GET", b"file_id=abc&_signature=deadbeef")):
				self.assertFalse(api._has_valid_signature())
			with patch(
				"frappe.request", request("POST", b"file_id=abc&_signature=sig:file_id=abc")
			):
				self.assertFalse(api._has_valid_signature())

	def test_api_download_serves_inline_for_authenticated_session(self) -> None:
		# The admin console calls the method with device credentials (no
		# signature); a logged-in owner/reviewer is authorized by the policy.
		file = self._downloadable_file()
		with self._serve_from_file(file):
			api.download_evidence(file.name)
		self.assertEqual(self.local.response.filecontent, b"private evidence")
		# Inline so the app/console can preview, not force a download.
		self.assertEqual(self.local.response.display_content_as, "inline")

	def test_api_download_serves_inline_for_a_signed_link(self) -> None:
		# The app fetches a signed URL with no session; the signature authorizes.
		file = self._downloadable_file()
		self.session.user = "Guest"
		with (
			patch.object(api, "_has_valid_signature", return_value=True),
			self._serve_from_file(file),
		):
			api.download_evidence(file.name)
		self.assertEqual(self.local.response.filecontent, b"private evidence")

	def test_api_download_rejects_unsigned_and_unlinked_files(self) -> None:
		file = self._downloadable_file()
		# Unsigned guest: no session and no signature.
		self.session.user = "Guest"
		with (
			patch.object(api, "_has_valid_signature", return_value=False),
			self.assertRaises(frappe.AuthenticationError),
		):
			api.download_evidence(file.name)
		# Unsigned session without subject read access is denied.
		self.session.user = "other"
		with (
			self._serve_from_file(file),
			patch.object(api, "_has_valid_signature", return_value=False),
			self.assertRaises(frappe.PermissionError),
		):
			api.download_evidence(file.name)
		# Even a valid signature cannot fetch a file that is not linked evidence.
		self.session.user = "alice"
		with (
			self._serve_from_file(file),
			patch.object(api, "_has_valid_signature", return_value=True),
			patch(
				"frappe.db.exists",
				side_effect=lambda doctype, filters=None, *a, **k: doctype != "Verification Document File",
			),
			self.assertRaises(frappe.PermissionError),
		):
			api.download_evidence(file.name)

	def test_push_delivery_is_app_scoped_private_and_retryable(self) -> None:
		with patch.object(CustomSubjectPolicy, "push_app", "custom-mobile"):
			self.create(
				source_app="custom",
				reference_doctype="Test Organization",
				reference_name="org-1",
				verification_purpose="Registration",
			)
		delivery = self.db.get_all("Verification Notification Delivery", filters={"channel": "Push"})[0]
		with patch(
			"flutter_utils.push_notifications.send_push_notifications", side_effect=RuntimeError
		) as send:
			notifications.deliver(delivery.name)
			self.assertEqual(send.call_args.kwargs["app"], "custom-mobile")
			self.assertTrue(send.call_args.kwargs["strict"])
			self.assertNotIn("Identity", send.call_args.args[2])
			# The client needs `type` to distinguish verification from call pushes
			# and to route the tap into the KYC screen.
			self.assertEqual(send.call_args.kwargs["data"]["type"], "verification")
			self.assertIn("submitted", send.call_args.args[1].lower())
			self.assertNotIn("Identity", send.call_args.args[1])
		self.assertEqual(self.db.get_doc(delivery.doctype, delivery.name).status, "Failed")

	def test_email_is_separate_policy_controlled_delivery(self) -> None:
		with patch.object(CustomSubjectPolicy, "email_enabled", True):
			self.create(
				source_app="custom",
				reference_doctype="Test Organization",
				reference_name="org-1",
				verification_purpose="Registration",
			)
		delivery = self.db.get_all("Verification Notification Delivery", filters={"channel": "Email"})[0]
		notifications.deliver(delivery.name)
		frappe.sendmail.assert_called_once()
		self.assertEqual(frappe.sendmail.call_args.kwargs["recipients"], ["alice@example.invalid"])
		self.assertTrue(frappe.sendmail.call_args.kwargs["delayed"])
		self.assertEqual(self.db.get_doc(delivery.doctype, delivery.name).status, "Delivered")

	def test_optional_channels_can_be_narrowed_per_action(self) -> None:
		with (
			patch.object(CustomSubjectPolicy, "email_enabled", True),
			patch.object(CustomSubjectPolicy, "channels", return_value=("In App",)),
		):
			self.create(
				source_app="custom",
				reference_doctype="Test Organization",
				reference_name="org-1",
				verification_purpose="Registration",
			)
		self.assertEqual(
			self.db.get_all("Verification Notification Delivery", filters={"channel": "Email"}), []
		)

	def test_reviewer_notification_explains_review_next_step(self) -> None:
		doc = self.create()
		_, description = notifications.message_for(doc, "Submitted", None, reviewer=True)
		self.assertIn("Please review", description)

	def test_notification_routes_reject_executable_or_evidence_links(self) -> None:
		for link in (
			"javascript:alert(1)",
			"//other.invalid/path",
			"/private/files/id.pdf",
			"https://test.invalid/api/method/flutter_utils.api.verification.download_evidence?file_id=x",
		):
			with self.assertRaises(frappe.ValidationError):
				notifications.client_link(link)
		self.assertEqual(
			notifications.client_link("https://app.invalid/verification/record"),
			"https://app.invalid/verification/record",
		)

	def test_strict_shared_push_worker_reports_failure(self) -> None:
		from flutter_utils.push_notifications import send_push_notifications

		self.db.docs[("Flutter Device Credential", "device")] = frappe._dict(
			doctype="Flutter Device Credential",
			name="device",
			user="alice",
			enabled=1,
			push_enabled=1,
			app="mobile",
			fcm_token="token",
		)
		with (
			patch("frappe.get_single", return_value=frappe._dict(enable_firebase_push=1)),
			patch("flutter_utils.firebase.get_firebase_app", return_value="firebase-app"),
			patch(
				"firebase_admin.messaging.send_each_for_multicast",
				return_value=SimpleNamespace(
					responses=[SimpleNamespace(success=False, exception=RuntimeError())]
				),
			),
		):
			with self.assertRaises(RuntimeError):
				send_push_notifications(["alice"], "Title", "Body", app="mobile", strict=True)
			self.assertIsNone(send_push_notifications(["alice"], "Title", "Body", app="mobile"))

	def test_realtime_targets_authorized_users_not_broad_rooms(self) -> None:
		doc = self.create()
		model.publish_updates(doc)
		for call in frappe.publish_realtime.call_args_list:
			self.assertIn("user", call.kwargs)
			self.assertNotIn("room", call.kwargs)

	def test_disabled_type_can_still_be_archived(self) -> None:
		doc = self.create()
		self.db.docs[("Verification Document Type", "National ID")].enabled = 0
		self.assertTrue(
			service.deactivate(doc.doctype, doc.name, "Archived", doc.document_revision).is_archived
		)

	def test_policy_evidence_validation_runs_after_upload_staging(self) -> None:
		def validate(doc: Any) -> None:
			if not evidence.current_evidence(doc):
				raise frappe.ValidationError("Evidence required")

		with patch.object(CustomSubjectPolicy, "validate_document", side_effect=validate):
			doc = self.create(
				source_app="custom",
				reference_doctype="Test Organization",
				reference_name="org-1",
				verification_purpose="Registration",
			)
			self.assertTrue(doc.files)
			with self.assertRaises(frappe.ValidationError):
				self.create(
					source_app="custom",
					reference_doctype="Test Organization",
					reference_name="org-1",
					verification_purpose="Registration",
					uploads=False,
				)

	def test_tightened_metadata_rules_do_not_prevent_withdrawal(self) -> None:
		doc = self.create()
		self.db.docs[("Verification Document Type", "National ID")].required_fields = "holder_name"
		self.assertTrue(
			service.deactivate(doc.doctype, doc.name, "Withdrawn", doc.document_revision).is_withdrawn
		)

	def test_policy_list_conditions_escape_user_input(self) -> None:
		with patch("frappe.get_roles", return_value=["Verification Reviewer"]):
			condition = policies.UserVerificationPolicy().query_condition("Verification Document", "o'neil")
		self.assertIn("o''neil", condition)
		self.assertIn("source_app", permissions.document_query_conditions("alice"))

	def test_notification_mixin_preserves_other_producers(self) -> None:
		class Original:
			def after_insert(self) -> str:
				return "original"

		class Notification(VerificationNotificationMixin, Original):
			for_user = "alice"

		self.assertEqual(Notification().after_insert(), "original")
		with (
			verification_notification(),
			patch("frappe.desk.doctype.notification_log.notification_log.set_notifications_as_unseen"),
		):
			Notification().after_insert()
		frappe.publish_realtime.assert_called_with("notification", after_commit=True, user="alice")

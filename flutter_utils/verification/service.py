from __future__ import annotations

import hashlib
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import PurePath
from typing import Any

import frappe
from frappe import _
from frappe.query_builder.functions import Count
from frappe.utils import cint, now_datetime, today

from flutter_utils.verification.evidence import (
	add_evidence,
	current_evidence,
	evidence_urls,
	get_max_file_size,
)
from flutter_utils.verification.model import RECORD_DOCTYPES, managed_write, validity
from flutter_utils.verification.policies import get_policy, require

CONTEXT_FIELDS = ("reference_doctype", "reference_name", "source_app", "verification_purpose")
COMMON_FIELDS = {"title", "description"}
DOCUMENT_FIELDS = COMMON_FIELDS | {
	"document_type",
	"document_number",
	"holder_name",
	"date_of_birth",
	"nationality",
	"issuing_authority",
	"issuing_country",
	"issuing_region",
	"issue_date",
	"expiry_date",
	"no_expiry",
}
CRITICAL_FIELDS = DOCUMENT_FIELDS - {"title", "description"}
TRANSITIONS = {
	"Approved": ("Pending", "Approved"),
	"Rejected": ("Pending", "Rejected"),
	"Revoked": ("Approved", "Pending"),
	"Reset": ("Rejected", "Pending"),
	"Resubmitted": ("Rejected", "Pending"),
}


@contextmanager
def atomic() -> Iterator[None]:
	"""Rollback local writes even when a trusted caller catches a validation error."""
	savepoint = f"verification_{frappe.generate_hash(length=12)}"
	callbacks = {
		name: list(getattr(frappe.db, name)._functions)
		for name in ("before_commit", "after_commit", "before_rollback", "after_rollback")
	}
	realtime = list(getattr(frappe.local, "_realtime_log", []))
	frappe.db.savepoint(savepoint)
	try:
		yield
	except Exception:
		frappe.db.rollback(save_point=savepoint)
		# Frappe savepoint rollback does not run filesystem watchers or discard queued
		# realtime. Restore prior transaction callbacks and run only new rollback cleanup.
		for name in ("before_rollback", "after_rollback"):
			for callback in list(getattr(frappe.db, name)._functions)[len(callbacks[name]) :]:
				try:
					callback()
				except Exception as exc:
					frappe.log_error(title="Verification rollback cleanup failed", message=type(exc).__name__)
		for name, functions in callbacks.items():
			getattr(frappe.db, name)._functions = deque(functions)
		if hasattr(frappe.local, "_realtime_log"):
			if realtime:
				frappe.local._realtime_log = realtime
			else:
				del frappe.local._realtime_log
		raise


def _doctype(doctype: str) -> None:
	if doctype not in RECORD_DOCTYPES:
		frappe.throw(_("Invalid verification record type."))


def lock_record(doctype: str, name: str, expected_revision: int | None = None) -> Any:
	_doctype(doctype)
	# All document mutations lock the containing request first, then the document.
	if doctype == "Verification Document":
		request = frappe.db.get_value(doctype, name, "verification_request")
		if request:
			frappe.db.get_value("Verification Request", request, "name", for_update=True)
	if not frappe.db.get_value(doctype, name, "name", for_update=True):
		frappe.throw(_("Verification record not found."), frappe.DoesNotExistError)
	doc = frappe.get_doc(doctype, name)
	if expected_revision is not None and cint(expected_revision) != cint(doc.document_revision):
		frappe.throw(
			_("This verification record changed. Refresh and try again."), frappe.TimestampMismatchError
		)
	return doc


def _persist(doc: Any, new: bool = False) -> None:
	with managed_write(doc.doctype, doc.name):
		if new:
			doc.insert(ignore_permissions=True, set_name=doc.name)
		else:
			doc.save(ignore_permissions=True)


def _reset_decision(doc: Any) -> None:
	doc.approval_status = "Pending"
	doc.rejection_code = None
	doc.rejection_reason = None
	doc.reviewed_by = None
	doc.reviewed_at = None
	doc.review_notes = None


def _active(doc: Any) -> None:
	if doc.is_archived or doc.is_withdrawn:
		frappe.throw(_("Archived or withdrawn verification records cannot be changed."))


def _self_review(doc: Any, user: str) -> bool:
	return doc.submitted_by == user or (doc.reference_doctype == "User" and doc.reference_name == user)


def save_record(
	doctype: str,
	data: dict[str, Any],
	record_id: str | None = None,
	expected_revision: int | None = None,
	idempotency_key: str | None = None,
	uploads: list[tuple[Any, str]] | None = None,
) -> Any:
	_doctype(doctype)
	allowed = DOCUMENT_FIELDS if doctype == "Verification Document" else COMMON_FIELDS
	creation_fields = set(CONTEXT_FIELDS) | (
		{"verification_request"} if doctype == "Verification Document" else set()
	)
	if set(data) - allowed - (creation_fields if not record_id else set()):
		frappe.throw(_("Unsupported or server-managed verification fields."))
	for field, value in data.items():
		if field == "no_expiry":
			if value not in (0, 1, False, True):
				frappe.throw(_("No-expiry must be a boolean."))
		elif value is not None and not isinstance(value, str):
			frappe.throw(_("Verification metadata must be text or null."))
	if "title" in data and not (data["title"] or "").strip():
		frappe.throw(_("Verification title is required."))
	if uploads and doctype != "Verification Document":
		frappe.throw(_("Evidence belongs to a document, not directly to a request."))
	with atomic():
		new = not record_id
		if new:
			if not isinstance(idempotency_key, str) or not idempotency_key or len(idempotency_key) > 128:
				frappe.throw(_("A creation idempotency key of at most 128 characters is required."))
			key = hashlib.sha256(f"{doctype}:{frappe.session.user}:{idempotency_key}".encode()).hexdigest()
			existing = frappe.db.get_value(doctype, {"idempotency_key": key}, "name")
			if existing:
				doc = lock_record(doctype, existing)
				require(doc, "read")
				if any(data.get(field) != doc.get(field) for field in CONTEXT_FIELDS):
					frappe.throw(_("Creation key was already used for another verification context."))
				return doc
			doc = frappe.get_doc({"doctype": doctype, **data})
			doc.name = key[:24]
			doc.idempotency_key = key
			doc.submitted_by = frappe.session.user
			doc.submitted_at = now_datetime()
			doc.submission_count = 1
			doc.document_revision = 1
			doc.approval_status = "Pending"
			policy = require(doc, "create")
			doc.tenant_doctype, doc.tenant_name = policy.tenant(doc)
			doc.contact_user = policy.contact(doc)
			doc.retention_until = policy.retention_until(doc)
			if doctype == "Verification Document" and doc.verification_request:
				request = lock_record("Verification Request", doc.verification_request)
				require(request, "edit")
				_active(request)
			_persist(doc, new=True)
			previous_status = None
			action = "Submitted"
		else:
			if expected_revision is None:
				frappe.throw(_("Expected revision is required for updates."))
			doc = lock_record(doctype, record_id, expected_revision)
			require(doc, "edit")
			_active(doc)
			previous_status = doc.approval_status
			changed = {field for field, value in data.items() if doc.get(field) != value}
			if not changed and not uploads:
				return doc
			critical = bool(uploads or changed.intersection(CRITICAL_FIELDS))
			old_evidence = list(current_evidence(doc)) if doctype == "Verification Document" else []
			doc.update(data)
			doc.document_revision += 1
			if doctype == "Verification Document" and not uploads:
				for row in old_evidence:
					doc.append(
						"files",
						{
							"file": row.file,
							"evidence_role": row.evidence_role,
							"revision": doc.document_revision,
							"uploaded_by": row.uploaded_by,
							"uploaded_at": row.uploaded_at,
							"content_hash": row.content_hash,
						},
					)
			if critical or doctype == "Verification Request":
				_reset_decision(doc)
			action = "Resubmitted" if previous_status == "Rejected" and critical else "Updated"
			if action == "Resubmitted":
				doc.last_resubmitted_at = now_datetime()
				doc.submission_count += 1
		if uploads:
			with managed_write(doc.doctype, doc.name):
				add_evidence(doc, uploads)
		if doctype == "Verification Document":
			validate_document_metadata(doc)
		_persist(doc)
		_emit(doc, action, previous_status)
		if doctype == "Verification Document":
			reevaluate_request(doc.verification_request)
		return doc


def check_transition(action: str, status: str, reason: str | None) -> str:
	if action not in TRANSITIONS or TRANSITIONS[action][0] != status:
		frappe.throw(_("This action is not available for the current approval status."))
	if action in {"Rejected", "Revoked"} and not (reason or "").strip():
		frappe.throw(_("A reason is required for this decision."))
	return TRANSITIONS[action][1]


def validate_document_metadata(doc: Any) -> None:
	"""Validate complete evidence once, after staging files, and before approval.

	Retractions/archive remain possible when administrators tighten requirements.
	"""
	policy = get_policy(doc.source_app)
	if doc.document_type not in policy.document_types(doc):
		frappe.throw(_("This document type is disabled or is not allowed for this subject."))
	config = frappe.get_doc("Verification Document Type", doc.document_type)
	for field in (config.required_fields or "").splitlines():
		if field.strip() and not doc.get(field.strip()):
			frappe.throw(_("Required document metadata is missing: {0}").format(field.strip()))
	if config.requires_expiry and not doc.expiry_date:
		frappe.throw(_("This document requires an expiry date."))
	rows = current_evidence(doc)
	if len(rows) > cint(config.max_files):
		frappe.throw(_("Evidence exceeds the document type file limit."))
	roles = {role.strip() for role in (config.allowed_roles or "").splitlines()}
	extensions = {
		value.strip().lower().lstrip(".") for value in (config.allowed_extensions or "").splitlines()
	}
	files = (
		frappe.get_all(
			"File",
			filters={"name": ["in", [row.file for row in rows]]},
			fields=[
				"name",
				"file_name",
				"file_size",
				"is_private",
				"attached_to_doctype",
				"attached_to_name",
			],
		)
		if rows
		else []
	)
	by_name = {file.name: file for file in files}
	limit = min(cint(config.max_file_size_mb) * 1024 * 1024, get_max_file_size()) if rows else 0
	for row in rows:
		file = by_name.get(row.file)
		if (
			not file
			or not file.is_private
			or file.attached_to_name != doc.name
			or file.attached_to_doctype != doc.doctype
		):
			frappe.throw(
				_("Current evidence for {0} is missing or does not belong to this document.").format(
					doc.document_type
				)
			)
		if (
			row.evidence_role not in roles
			or PurePath(file.file_name).suffix.lower().lstrip(".") not in extensions
		):
			frappe.throw(
				_("Existing evidence for {0} does not match the document type requirements.").format(
					doc.document_type
				)
			)
		if cint(file.file_size) > limit:
			frappe.throw(_("Existing evidence exceeds the document type upload limit."))
	policy.validate_document(doc)


def transition(
	doctype: str,
	record_id: str,
	action: str,
	expected_revision: int,
	reason: str | None = None,
	review_notes: str | None = None,
) -> Any:
	with atomic():
		doc = lock_record(doctype, record_id, expected_revision)
		require(doc, "edit" if action == "Resubmitted" else "review")
		_active(doc)
		if action != "Resubmitted" and _self_review(doc, frappe.session.user):
			frappe.throw(_("You cannot review your own submission."), frappe.PermissionError)
		before = doc.approval_status
		after = check_transition(action, before, reason)
		if action == "Approved":
			if doctype == "Verification Document":
				validate_document_metadata(doc)
				if not current_evidence(doc) or validity(doc.expiry_date) == "Expired":
					frappe.throw(_("Approval requires current, unexpired evidence."))
			else:
				validate_request_ready(doc)
		_reset_decision(doc)
		doc.approval_status = after
		doc.document_revision += 1
		if doctype == "Verification Document":
			_copy_evidence_revision(doc, doc.document_revision - 1)
		if action == "Rejected":
			doc.rejection_reason = (reason or "").strip()
		if action == "Resubmitted":
			doc.last_resubmitted_at = now_datetime()
			doc.submission_count += 1
		else:
			doc.reviewed_by = frappe.session.user
			doc.reviewed_at = now_datetime()
			doc.review_notes = review_notes
		_persist(doc)
		_emit(doc, action, before, reason)
		if doctype == "Verification Document":
			reevaluate_request(doc.verification_request)
		return doc


def _copy_evidence_revision(doc: Any, previous_revision: int) -> None:
	for row in list(doc.files):
		if cint(row.revision) == previous_revision:
			doc.append(
				"files",
				{
					"file": row.file,
					"evidence_role": row.evidence_role,
					"revision": doc.document_revision,
					"uploaded_by": row.uploaded_by,
					"uploaded_at": row.uploaded_at,
					"content_hash": row.content_hash,
				},
			)


def deactivate(doctype: str, record_id: str, action: str, expected_revision: int) -> Any:
	if action not in {"Withdrawn", "Archived"}:
		frappe.throw(_("Invalid verification removal action."))
	with atomic():
		doc = lock_record(doctype, record_id, expected_revision)
		require(doc, "withdraw" if action == "Withdrawn" else "archive")
		_active(doc)
		doc.set("is_withdrawn" if action == "Withdrawn" else "is_archived", 1)
		doc.set("withdrawn_at" if action == "Withdrawn" else "archived_at", now_datetime())
		doc.document_revision += 1
		if doctype == "Verification Document":
			_copy_evidence_revision(doc, doc.document_revision - 1)
		_persist(doc)
		_emit(doc, action, doc.approval_status)
		if doctype == "Verification Document":
			reevaluate_request(doc.verification_request)
		return doc


def assign_reviewer(doctype: str, record_id: str, user: str, expected_revision: int) -> Any:
	with atomic():
		doc = lock_record(doctype, record_id, expected_revision)
		policy = require(doc, "assign")
		_active(doc)
		if not frappe.db.get_value("User", user, "enabled") or not policy.can(doc, "review", user):
			frappe.throw(_("The assigned user is not authorized to review this subject."))
		if _self_review(doc, user):
			frappe.throw(_("A submitter cannot review their own submission."))
		if doc.assigned_reviewer == user:
			return doc
		doc.assigned_reviewer = user
		doc.document_revision += 1
		if doctype == "Verification Document":
			_copy_evidence_revision(doc, doc.document_revision - 1)
		_persist(doc)
		_emit(doc, "Reviewer Assigned", doc.approval_status)
		return doc


def usable_document_counts(request_ids: list[str]) -> dict[str, dict[str, int]]:
	"""Aggregate one page of request readiness in one bounded-result query."""
	if not request_ids:
		return {}
	document = frappe.qb.DocType("Verification Document")
	file = frappe.qb.DocType("File")
	rows = (
		frappe.qb.from_(document)
		.inner_join(file)
		.on(file.name == document.primary_file)
		.select(
			document.verification_request, document.document_type, Count(document.name).as_("document_count")
		)
		.where(
			document.verification_request.isin(request_ids)
			& (document.approval_status == "Approved")
			& (document.is_archived == 0)
			& (document.is_withdrawn == 0)
			& (document.expiry_date.isnull() | (document.expiry_date >= today()))
			& (file.is_private == 1)
			& (file.attached_to_doctype == "Verification Document")
			& (file.attached_to_name == document.name)
		)
		.groupby(document.verification_request, document.document_type)
		.run(as_dict=True)
	)
	result: dict[str, dict[str, int]] = {}
	for row in rows:
		result.setdefault(row.verification_request, {})[row.document_type] = cint(row.document_count)
	return result


def request_requirements(request: Any, counts: dict[str, int] | None = None) -> dict[str, int]:
	if counts is None:
		counts = usable_document_counts([request.name]).get(request.name, {})
	required = get_policy(request.source_app).required_documents(request)
	if not counts and not required:
		return {"Any valid approved document": 1}
	return {
		type_name: max(0, minimum - counts.get(type_name, 0))
		for type_name, minimum in required.items()
		if minimum > counts.get(type_name, 0)
	}


def validate_request_ready(request: Any) -> None:
	if request_requirements(request):
		frappe.throw(_("Required documents must be approved and currently valid before request approval."))


def reevaluate_request(name: str | None) -> None:
	if not name:
		return
	request = lock_record("Verification Request", name)
	if request.is_archived or request.is_withdrawn:
		return
	if request.approval_status == "Approved" and request_requirements(request):
		_reset_decision(request)
		request.document_revision += 1
		_persist(request)
		_emit(request, "Evidence Invalidated", "Approved", "Required evidence is no longer valid.")


def _emit(doc: Any, action: str, before: str | None, reason: str | None = None) -> None:
	from flutter_utils.verification.notifications import record_event

	event = record_event(doc, action, before, reason)
	get_policy(doc.source_app).after_transition(doc, event)


def allowed_actions(doc: Any) -> list[str]:
	policy = get_policy(doc.source_app)
	user = frappe.session.user
	if doc.is_archived or doc.is_withdrawn:
		return []
	actions = []
	if policy.can(doc, "edit", user):
		actions.append("edit")
		if doc.approval_status == "Rejected":
			actions.append("resubmit")
	if policy.can(doc, "review", user) and not _self_review(doc, user):
		actions.extend(
			{"Pending": ["approve", "reject"], "Approved": ["revoke"], "Rejected": ["reset"]}[
				doc.approval_status
			]
		)
	for action in ("withdraw", "archive", "assign"):
		if policy.can(doc, action, user):
			actions.append(action)
	return actions


def serialize(
	doc: Any, file_map: dict[str, Any] | None = None, counts: dict[str, int] | None = None
) -> dict[str, Any]:
	policy = require(doc, "read")
	fields = (
		*CONTEXT_FIELDS,
		"title",
		"description",
		"contact_user",
		"tenant_doctype",
		"tenant_name",
		"document_revision",
		"submitted_by",
		"submitted_at",
		"last_resubmitted_at",
		"submission_count",
		"approval_status",
		"rejection_code",
		"rejection_reason",
		"reviewed_by",
		"reviewed_at",
		"assigned_reviewer",
		"is_archived",
		"is_withdrawn",
		"archived_at",
		"withdrawn_at",
	)
	result = {field: doc.get(field) for field in fields}
	result.update({"id": doc.name, "doctype": doc.doctype, "allowed_actions": allowed_actions(doc)})
	if policy.can(doc, "history_internal", frappe.session.user):
		result["review_notes"] = doc.review_notes
	if doc.doctype == "Verification Document":
		result.update({field: doc.get(field) for field in DOCUMENT_FIELDS - COMMON_FIELDS})
		result["validity_status"] = validity(doc.expiry_date)
		rows = current_evidence(doc)
		if file_map is None:
			files = frappe.get_all(
				"File",
				filters={"name": ["in", [row.file for row in rows]]},
				fields=["name", "file_name", "is_private", "attached_to_doctype", "attached_to_name"],
			)
			file_map = {file.name: file for file in files}
		files = [
			file_map[row.file]
			for row in rows
			if row.file in file_map and file_map[row.file].attached_to_name == doc.name
		]
		urls = evidence_urls(files)
		result["files"] = [
			{
				"id": row.file,
				"role": row.evidence_role,
				"revision": row.revision,
				"filename": file_map[row.file].file_name if row.file in file_map else None,
				"url": urls.get(row.file),
				"available": bool(urls.get(row.file)),
			}
			for row in rows
		]
		if (not files or validity(doc.expiry_date) == "Expired") and "approve" in result["allowed_actions"]:
			result["allowed_actions"].remove("approve")
		result["verification_request"] = doc.verification_request
	else:
		result["missing_requirements"] = request_requirements(doc, counts)
		result["ready_for_approval"] = not bool(result["missing_requirements"])
		if result["missing_requirements"] and "approve" in result["allowed_actions"]:
			result["allowed_actions"].remove("approve")
	return result

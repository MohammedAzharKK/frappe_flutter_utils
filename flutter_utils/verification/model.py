from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import add_days, cint, getdate, today

RECORD_DOCTYPES = ("Verification Document", "Verification Request")
_write_scope: ContextVar[tuple[str, str] | None] = ContextVar("verification_write_scope", default=None)
_internal_scope: ContextVar[bool] = ContextVar("verification_internal_scope", default=False)


@contextmanager
def managed_write(doctype: str, name: str) -> Iterator[None]:
	token = _write_scope.set((doctype, name))
	try:
		yield
	finally:
		_write_scope.reset(token)


@contextmanager
def internal_write() -> Iterator[None]:
	token = _internal_scope.set(True)
	try:
		yield
	finally:
		_internal_scope.reset(token)


def is_managed(doctype: str, name: str) -> bool:
	return _write_scope.get() == (doctype, name)


def validity(expiry_date: str | None, warning_days: int = 30) -> str:
	if not expiry_date:
		return "No Expiry"
	expiry = getdate(expiry_date)
	if expiry < getdate(today()):
		return "Expired"
	if expiry <= getdate(add_days(today(), warning_days)):
		return "Expiring Soon"
	return "Active"


def publish_updates(doc: Any, after_commit: bool = True) -> None:
	from flutter_utils.verification.policies import can_access, get_policy

	policy = get_policy(doc.source_app)
	requested = [user for user in set(policy.recipients(doc, "Updated")) if user and user != "Guest"]
	enabled = (
		frappe.get_all("User", filters={"name": ["in", requested], "enabled": 1}, pluck="name")
		if requested
		else []
	)
	for user in enabled:
		if can_access(doc, "read", user, policy):
			frappe.publish_realtime(
				"doc_update",
				{
					"modified": doc.modified,
					"doctype": doc.doctype,
					"name": doc.name,
				},
				user=user,
				after_commit=after_commit,
			)
			frappe.publish_realtime(
				"list_update",
				{
					"doctype": doc.doctype,
					"name": doc.name,
					"user": frappe.session.user,
				},
				user=user,
				after_commit=after_commit,
			)


class VerificationRecord(Document):
	def notify_update(self) -> None:
		# Default doctype broadcasts reveal cross-tenant record IDs. Preserve standard
		# event names but target only policy-authorized users, never a shared room.
		publish_updates(self)

	def db_set(self, *args: Any, **kwargs: Any) -> None:
		frappe.throw(
			_("Use the verification service; direct field updates are not supported."), frappe.PermissionError
		)

	def before_rename(self, old: str, new: str, merge: bool = False) -> None:
		frappe.throw(_("Verification record IDs cannot be changed."), frappe.PermissionError)

	def validate(self) -> None:
		if not is_managed(self.doctype, self.name):
			frappe.throw(
				_("Use the verification service to create or change this record."), frappe.PermissionError
			)
		from flutter_utils.verification.policies import get_policy

		policy = get_policy(self.source_app)
		policy.validate_subject(self)
		if (self.tenant_doctype or None, self.tenant_name or None) != policy.tenant(self):
			frappe.throw(_("Invalid verification tenant scope."), frappe.PermissionError)
		if self.approval_status not in {"Pending", "Approved", "Rejected"}:
			frappe.throw(_("Invalid approval status."))
		if self.approval_status == "Rejected" and not (self.rejection_reason or "").strip():
			frappe.throw(_("Rejection reason is required."))
		if self.doctype == "Verification Document":
			self._validate_document(policy)

	def _validate_document(self, policy: Any) -> None:
		config = frappe.get_doc("Verification Document Type", self.document_type)
		if self.no_expiry and self.expiry_date:
			frappe.throw(_("A no-expiry document cannot have an expiry date."))
		if self.issue_date and self.expiry_date and getdate(self.issue_date) > getdate(self.expiry_date):
			frappe.throw(_("Issue date cannot be after expiry date."))
		if self.verification_request:
			request = frappe.get_doc("Verification Request", self.verification_request)
			for field in ("reference_doctype", "reference_name", "source_app", "verification_purpose"):
				if self.get(field) != request.get(field):
					frappe.throw(_("Document and request must have the same subject and context."))
		self.validity_status = validity(self.expiry_date, cint(config.warning_days) or 30)

	def on_trash(self) -> None:
		frappe.throw(
			_("Verification records must be withdrawn or archived, not deleted."), frappe.PermissionError
		)


class ImmutableVerificationRecord(Document):
	def validate(self) -> None:
		if not _internal_scope.get():
			frappe.throw(_("This verification record is server-managed."), frappe.PermissionError)
		if self.doctype == "Verification Review Log" and not self.is_new():
			frappe.throw(_("Verification audit events are immutable."), frappe.PermissionError)

	def on_trash(self) -> None:
		frappe.throw(
			_("Verification history and delivery records cannot be deleted."), frappe.PermissionError
		)

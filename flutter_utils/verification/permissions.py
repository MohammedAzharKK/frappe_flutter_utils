from __future__ import annotations

from typing import Any

import frappe

from flutter_utils.verification.model import RECORD_DOCTYPES, is_managed
from flutter_utils.verification.policies import can_access, get_policy


def has_permission(doc: Any, ptype: str | None = None, user: str | None = None) -> bool:
	user = user or frappe.session.user
	if user == "Guest":
		return False
	if doc.doctype in {"Verification Review Log", "Verification Notification Delivery"}:
		# Raw history contains internal recipients/reasons. Use the filtered history API.
		return False
	policy = get_policy(doc.source_app)
	if ptype in {None, "read", "select"}:
		# Raw Desk/REST records contain internal fields. Applicant reads use the safe API.
		return can_access(doc, "history_internal", user, policy)
	return False  # All mutations go through the service, including Desk actions.


def query_conditions(user: str | None = None, doctype: str | None = None) -> str:
	user = user or frappe.session.user
	if not doctype or doctype not in RECORD_DOCTYPES or user == "Guest":
		return "1=0"
	sources = {"flutter_utils", *(frappe.get_hooks("verification_policies") or {}).keys()}
	conditions = []
	for source in sorted(sources):
		policy = get_policy(source)
		condition = policy.query_condition(doctype, user)
		conditions.append(f"(`tab{doctype}`.source_app = {frappe.db.escape(source)} AND ({condition}))")
	return " OR ".join(conditions) or "1=0"


def document_query_conditions(user: str | None = None) -> str:
	return query_conditions(user, "Verification Document")


def request_query_conditions(user: str | None = None) -> str:
	return query_conditions(user, "Verification Request")


def deny_query(user: str | None = None) -> str:
	return "1=0"


def file_query_conditions(user: str | None = None) -> str:
	conditions = query_conditions(user, "Verification Document")
	return (
		"(`tabFile`.attached_to_doctype IS NULL OR `tabFile`.attached_to_doctype NOT IN "
		"('Verification Document', 'Verification Request') OR "
		"(`tabFile`.attached_to_doctype = 'Verification Document' AND `tabFile`.is_private = 1 "
		f"AND `tabFile`.attached_to_name IN (SELECT name FROM `tabVerification Document` WHERE {conditions})))"
	)


def file_permission(doc: Any, ptype: str | None = None, user: str | None = None) -> bool | None:
	if doc.attached_to_doctype not in RECORD_DOCTYPES:
		return None
	if (user or frappe.session.user) == "Guest":
		return False
	if ptype not in {None, "read", "select"}:
		return False
	if not doc.is_private or not doc.attached_to_name:
		return False
	record = frappe.get_doc(doc.attached_to_doctype, doc.attached_to_name)
	return can_access(record, "read", user or frappe.session.user)


def protect_file(doc: Any, method: str | None = None) -> None:
	old = doc.get_doc_before_save()
	for record in (doc, old):
		if record and record.attached_to_doctype in RECORD_DOCTYPES:
			if not is_managed(record.attached_to_doctype, record.attached_to_name):
				frappe.throw("Use the verification service to manage evidence.", frappe.PermissionError)
			if not doc.is_private:
				frappe.throw("Verification evidence must remain private.", frappe.PermissionError)

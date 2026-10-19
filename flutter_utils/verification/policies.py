from __future__ import annotations

from typing import Any

import frappe
from frappe import _


class VerificationPolicy:
	"""Trusted app contract. Override explicitly; permissions deny by default."""

	reference_doctypes: tuple[str, ...] = ()
	purposes: tuple[str, ...] = ()
	push_app: str | None = None
	email_enabled = False

	def validate_subject(self, doc: Any) -> None:
		if (
			doc.reference_doctype not in self.reference_doctypes
			or doc.verification_purpose not in self.purposes
		):
			frappe.throw(_("Unsupported verification subject or purpose."))
		if not frappe.db.exists(doc.reference_doctype, doc.reference_name):
			frappe.throw(_("Verification subject does not exist."))

	def tenant(self, doc: Any) -> tuple[str | None, str | None]:
		return None, None

	def contact(self, doc: Any) -> str | None:
		return None

	def retention_until(self, doc: Any) -> str | None:
		return None

	def can(self, doc: Any, action: str, user: str) -> bool:
		return False

	def query_condition(self, doctype: str, user: str) -> str:
		"""Trusted SQL predicate for internal Desk reads; escape interpolated values.

		Must be at least as restrictive as can(doc, 'history_internal', user).
		Applicant lists use the serialized service API, not raw Desk records.
		"""
		return "1=0"

	def list_filters(self, user: str) -> dict[str, Any] | None:
		"""Exact readable scope for API lists; None requires an explicit subject.

		Filters must guarantee read access, not merely narrow a global candidate scan.
		"""
		return None

	def document_types(self, doc: Any) -> list[str]:
		return []

	def required_documents(self, request: Any) -> dict[str, int]:
		"""Map required document type names to minimum valid approved document counts."""
		return {}

	def validate_document(self, doc: Any) -> None:
		pass

	def recipients(self, doc: Any, action: str) -> list[str]:
		return [doc.submitted_by] if doc.submitted_by else []

	def channels(self, doc: Any, action: str, user: str) -> tuple[str, ...]:
		"""Override to narrow optional channels per action/user; In App is mandatory."""
		channels = ["In App"]
		if self.push_app:
			channels.append("Push")
		if self.email_enabled:
			channels.append("Email")
		return tuple(channels)

	def link(self, doc: Any, user: str) -> str:
		return f"/app/{frappe.scrub(doc.doctype).replace('_', '-')}/{doc.name}"

	def after_transition(self, doc: Any, event: Any) -> None:
		"""Local transactional business effects only; enqueue remote effects after commit."""
		pass


class UserVerificationPolicy(VerificationPolicy):
	reference_doctypes = ("User",)
	purposes = ("KYC", "Profile", "Credentials")

	def contact(self, doc: Any) -> str | None:
		return doc.reference_name

	def required_documents(self, request: Any) -> dict[str, int]:
		return {"National ID": 1} if request.verification_purpose == "KYC" else {}

	def list_filters(self, user: str) -> dict[str, Any]:
		if {"System Manager", "Verification Reviewer"}.intersection(frappe.get_roles(user)):
			return {"reference_doctype": "User"}
		return {"reference_doctype": "User", "reference_name": user}

	def can(self, doc: Any, action: str, user: str) -> bool:
		if user == "Guest":
			return False
		reviewer = bool({"System Manager", "Verification Reviewer"}.intersection(frappe.get_roles(user)))
		if action in {"review", "history_internal", "assign"}:
			return reviewer and doc.reference_name != user and doc.submitted_by != user
		if action == "read":
			return reviewer or doc.reference_name == user
		return action in {"create", "edit", "withdraw", "archive"} and doc.reference_name == user

	def query_condition(self, doctype: str, user: str) -> str:
		if user == "Guest":
			return "1=0"
		if {"System Manager", "Verification Reviewer"}.intersection(frappe.get_roles(user)):
			return (
				f"`tab{doctype}`.reference_doctype = 'User' "
				f"AND `tab{doctype}`.reference_name != {frappe.db.escape(user)} "
				f"AND `tab{doctype}`.submitted_by != {frappe.db.escape(user)}"
			)
		return "1=0"

	def document_types(self, doc: Any) -> list[str]:
		return frappe.get_all("Verification Document Type", filters={"enabled": 1}, pluck="name")

	def recipients(self, doc: Any, action: str) -> list[str]:
		users = [doc.reference_name, doc.submitted_by]
		if doc.assigned_reviewer:
			users.append(doc.assigned_reviewer)
		elif action in {"Submitted", "Updated", "Resubmitted", "Reset", "Evidence Invalidated"}:
			users.extend(
				frappe.get_all(
					"Has Role",
					filters={
						"role": ["in", ["System Manager", "Verification Reviewer"]],
						"parenttype": "User",
					},
					pluck="parent",
				)
			)
		return [user for user in users if user]


def get_policy(source_app: str) -> VerificationPolicy:
	if source_app == "flutter_utils":
		return UserVerificationPolicy()
	# Hook values are dotted class paths, never supplied by the API caller.
	registrations = frappe.get_hooks("verification_policies") or {}
	paths = registrations.get(source_app, [])
	if isinstance(paths, str):
		paths = [paths]
	if len(paths) != 1:
		frappe.throw(_("Verification integration is unregistered or ambiguous."), frappe.PermissionError)
	policy = frappe.get_attr(paths[0])()
	if not isinstance(policy, VerificationPolicy):
		frappe.throw(_("Invalid verification policy configuration."))
	return policy


def can_access(
	doc: Any, action: str, user: str, policy: VerificationPolicy | None = None, *, validated: bool = False
) -> bool:
	if user == "Guest":
		return False
	policy = policy or get_policy(doc.source_app)
	try:
		if not validated:
			policy.validate_subject(doc)
		if doc.get("name") and (
			doc.get("tenant_doctype") or None,
			doc.get("tenant_name") or None,
		) != policy.tenant(doc):
			return False
		return policy.can(doc, action, user)
	except frappe.PermissionError, frappe.ValidationError, frappe.DoesNotExistError:
		return False


def require(doc: Any, action: str, user: str | None = None) -> VerificationPolicy:
	user = user or frappe.session.user
	policy = get_policy(doc.source_app)
	policy.validate_subject(doc)
	if action == "create" or not doc.get("name"):
		doc.tenant_doctype, doc.tenant_name = policy.tenant(doc)
	if not can_access(doc, action, user, policy, validated=True):
		frappe.throw(_("Not permitted to perform this verification action."), frappe.PermissionError)
	return policy

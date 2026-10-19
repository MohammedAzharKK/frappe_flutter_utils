from __future__ import annotations

import frappe
from frappe import _

from flutter_utils.verification.model import RECORD_DOCTYPES
from flutter_utils.verification.permissions import file_permission, protect_file


class VerificationFileMixin:
	def before_insert(self) -> None:
		# Core File.before_insert writes the blob before before_validate hooks run.
		protect_file(self)
		return super().before_insert()

	def on_trash(self) -> None:
		# Core File.on_trash deletes bytes before doc_event hooks; guard first.
		if self.attached_to_doctype in RECORD_DOCTYPES:
			frappe.throw(_("Verification evidence must be retained, not deleted."), frappe.PermissionError)
		return super().on_trash()

	def is_downloadable(self) -> bool:
		# Frappe's native download finder calls File.is_downloadable directly, not
		# frappe.has_permission. Enforce subject scope on this path as well.
		if self.attached_to_doctype in RECORD_DOCTYPES:
			return bool(file_permission(self, "read", frappe.session.user))
		return super().is_downloadable()

	def before_rename(self, old: str, new: str, merge: bool = False) -> None:
		if self.attached_to_doctype in RECORD_DOCTYPES:
			frappe.throw(_("Verification evidence IDs cannot be changed."), frappe.PermissionError)
		parent = getattr(super(), "before_rename", None)
		if parent:
			parent(old, new, merge)

import frappe
from frappe import _
from frappe.model.document import Document

from flutter_utils.verification.model import is_managed


class VerificationDocumentFile(Document):
	def validate(self) -> None:
		if self.parenttype != "Verification Document" or not is_managed(self.parenttype, self.parent):
			frappe.throw(_("Evidence revisions are server-managed."), frappe.PermissionError)

	def on_trash(self) -> None:
		frappe.throw(_("Evidence revisions cannot be deleted."), frappe.PermissionError)

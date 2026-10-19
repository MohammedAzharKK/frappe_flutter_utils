import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint

from flutter_utils.verification.media import SUPPORTED_EXTENSIONS

METADATA_FIELDS = {
	"document_number",
	"holder_name",
	"date_of_birth",
	"nationality",
	"issuing_authority",
	"issuing_country",
	"issuing_region",
	"issue_date",
	"expiry_date",
}


class VerificationDocumentType(Document):
	def validate(self) -> None:
		if any(
			field.strip() and field.strip() not in METADATA_FIELDS
			for field in (self.required_fields or "").splitlines()
		):
			frappe.throw(_("Required fields must be supported verification metadata fields."))
		extensions = {
			value.strip().lower().lstrip(".")
			for value in (self.allowed_extensions or "").splitlines()
			if value.strip()
		}
		if not extensions or extensions - SUPPORTED_EXTENSIONS:
			frappe.throw(
				_(
					"Evidence formats must be PDF, PNG, JPG, JPEG, WebP, GIF, BMP, MP4, M4V, MOV, 3GP, or 3G2."
				)
			)
		if cint(self.max_files) < 1 or cint(self.max_files) > 20:
			frappe.throw(_("Maximum evidence files must be between 1 and 20."))
		if cint(self.max_file_size_mb) < 1 or cint(self.warning_days) < 1:
			frappe.throw(_("File size and expiry warning days must be positive."))

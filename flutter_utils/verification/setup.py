import frappe

DOCUMENT_TYPES = (
	"National ID",
	"Passport",
	"Residence Permit",
	"Driving License",
	"Proof of Address",
	"Selfie / Identity Photo",
	"Professional License",
	"Certification",
	"Training",
	"Business Registration",
	"Government Authorization",
	"Affiliation",
	"Insurance",
	"Medical",
	"Compliance",
	"Contract",
	"Other",
)


def seed_defaults() -> None:
	"""Idempotent install/migrate hook; never overwrite administrator configuration."""
	if not frappe.db.exists("DocType", "Verification Document Type"):
		return
	if not frappe.db.exists("Role", "Verification Reviewer"):
		frappe.get_doc({"doctype": "Role", "role_name": "Verification Reviewer", "desk_access": 1}).insert(
			ignore_permissions=True
		)
	for title in DOCUMENT_TYPES:
		if not frappe.db.exists("Verification Document Type", title):
			frappe.get_doc(
				{
					"doctype": "Verification Document Type",
					"title": title,
					"enabled": 1,
					"allowed_extensions": "pdf\njpg\njpeg\npng",
					"allowed_roles": "Front\nBack\nSelfie\nSupporting Document\nAdditional Page",
					"max_files": 5,
					"max_file_size_mb": 10,
					"warning_days": 30,
				}
			).insert(ignore_permissions=True)

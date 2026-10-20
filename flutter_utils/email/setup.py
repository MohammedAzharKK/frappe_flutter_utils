import frappe

from flutter_utils.email import SENDGRID_SERVICE


def ensure_sendgrid_service() -> None:
	"""Preserve site-specific service options while adopting the legacy integration."""
	field = frappe.get_meta("Email Account").get_field("service")
	options = (field.options or "").split("\n")
	if SENDGRID_SERVICE in options:
		return
	options.append(SENDGRID_SERVICE)
	frappe.make_property_setter(
		{
			"doctype": "Email Account",
			"fieldname": "service",
			"property": "options",
			"value": "\n".join(options),
			"property_type": "Text",
		}
	)

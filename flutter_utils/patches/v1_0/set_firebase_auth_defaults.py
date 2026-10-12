import frappe


def execute() -> None:
	frappe.db.set_single_value("Flutter Utils Settings", "firebase_auto_create_users", 1)
	frappe.db.set_single_value("Flutter Utils Settings", "firebase_check_revoked_tokens", 0)

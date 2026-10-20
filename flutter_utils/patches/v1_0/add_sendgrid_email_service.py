"""Make SendGrid available without requiring the deprecated frappe_sendgrid app."""

from flutter_utils.email.setup import ensure_sendgrid_service


def execute() -> None:
	ensure_sendgrid_service()

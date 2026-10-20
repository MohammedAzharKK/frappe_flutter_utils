from typing import Any

import frappe
import requests
from frappe import _
from frappe.email.doctype.email_queue.email_queue import EmailQueue

from flutter_utils.email import SENDGRID_SERVICE
from flutter_utils.email.payload import _build_payload


def send_via_sendgrid(queue_doc: EmailQueue, sender: str, recipient: str, message: bytes | str) -> None:
	"""Worker-only transport; normal queue routing never installs a global send hook."""
	if getattr(frappe.local, "request", None):
		frappe.throw(_("SendGrid delivery must run through the background Email Queue."))
	account = queue_doc.get_email_account(raise_error=True)
	if account.service != SENDGRID_SERVICE:
		frappe.throw(_("This email account does not use SendGrid HTTP."))
	api_key = account.get_password("password", raise_exception=False) or frappe.conf.get("sendgrid_api_key")
	if not api_key:
		frappe.throw(_("SendGrid API key is not configured for the outgoing email account."))
	_post_to_sendgrid(api_key, _build_payload(sender, recipient, message))


def _post_to_sendgrid(api_key: str, payload: dict[str, Any]) -> None:
	try:
		response = requests.post(
			"https://api.sendgrid.com/v3/mail/send",
			json=payload,
			headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
			timeout=(5, 30),
			allow_redirects=False,
		)
	except requests.RequestException:
		# Do not persist provider responses, recipient data, or credentials in queue errors.
		response = None
	if response is None:
		frappe.throw(_("SendGrid could not be reached. Check Email Queue before retrying."))
	if response.status_code not in (200, 202):
		frappe.throw(
			_("SendGrid rejected the email (HTTP {0}). Check the account configuration.").format(
				response.status_code
			)
		)

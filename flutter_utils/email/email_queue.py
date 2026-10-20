from typing import Any

import frappe
from frappe.email.doctype.email_queue.email_queue import SendMailContext

from flutter_utils.email import SENDGRID_SERVICE
from flutter_utils.email.sendgrid import send_via_sendgrid


def send_queued_email(name: str, force_send: bool = False) -> None:
	"""Trusted worker entry point, not a whitelisted user action.

	OTP emails may be queued by Guest. The Desk send_now endpoint checks the
	caller's document permissions, which is inappropriate for worker delivery.
	"""
	frappe.get_doc("Email Queue", name).send(force_send=force_send)


class SendgridEmailQueueMixin:
	def send(
		self, smtp_server_instance: Any = None, frappe_mail_client: Any = None, force_send: bool = False
	) -> None:
		if not self.can_send_now() and not force_send:
			return
		account = self.get_email_account(raise_error=True)
		if account.service != SENDGRID_SERVICE:
			return super().send(smtp_server_instance, frappe_mail_client, force_send)

		if getattr(frappe.local, "request", None):
			# Even sendmail(now=True) must not perform outbound HTTP inside a request.
			frappe.enqueue(
				"flutter_utils.email.email_queue.send_queued_email",
				name=self.name,
				force_send=force_send,
				queue="short",
				enqueue_after_commit=True,
			)
			return

		# Serialize scheduler and immediate-send jobs, then refresh per-recipient progress.
		with frappe.cache.lock(f"flutter_utils:sendgrid:{self.name}", timeout=3600, blocking_timeout=1):
			self.reload()
			if not self.can_send_now() and not force_send:
				return
			with SendMailContext(self) as ctx:
				ctx.email_account_doc = self.get_email_account(raise_error=True)
				last_message = None
				for recipient in self.recipients:
					if recipient.is_mail_sent():
						continue
					last_message = ctx.build_message(recipient.recipient)
					if not frappe.in_test or frappe.flags.testing_email:
						send_via_sendgrid(self, self.sender, recipient.recipient, last_message)
					ctx.update_recipient_status_to_sent(recipient)
				if frappe.in_test and not frappe.flags.testing_email:
					frappe.flags.sent_mail = last_message
				elif last_message and ctx.email_account_doc.append_emails_to_sent_folder:
					ctx.email_account_doc.append_email_to_sent_folder(last_message)

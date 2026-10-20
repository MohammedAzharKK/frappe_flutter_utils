"""Offline transport and migration tests. No database or real email required."""

import base64
import importlib.util
import unittest
from email.message import EmailMessage
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import requests
from frappe.email.doctype.email_queue.email_queue import SendMailContext

from flutter_utils.email import email_account, email_queue, sendgrid, setup
from flutter_utils.email.payload import _build_payload


class TestSendgridPayload(unittest.TestCase):
	def message(self) -> EmailMessage:
		msg = EmailMessage()
		msg["Subject"] = "Hello مرحبا"
		msg["Reply-To"] = "Support Team <support@example.com>"
		msg.set_content("Plain body")
		msg.add_alternative("<p>HTML body</p>", subtype="html")
		return msg

	def test_headers_bodies_and_private_envelope(self) -> None:
		msg = self.message()
		msg["Bcc"] = "hidden@example.com"
		payload = _build_payload("Sender Name <sender@example.com>", "target@example.com", msg.as_bytes())
		self.assertEqual(payload["from"], {"email": "sender@example.com", "name": "Sender Name"})
		self.assertEqual(payload["reply_to"], {"email": "support@example.com", "name": "Support Team"})
		self.assertEqual(payload["subject"], "Hello مرحبا")
		self.assertEqual([entry["type"] for entry in payload["content"]], ["text/plain", "text/html"])
		self.assertEqual(payload["personalizations"], [{"to": [{"email": "target@example.com"}]}])

	def test_attachments_and_inline_content_id(self) -> None:
		msg = self.message()
		msg.get_payload()[1].add_related(
			b"image", maintype="image", subtype="png", cid="<logo>", disposition="inline"
		)
		msg.add_attachment(b"file bytes", maintype="application", subtype="pdf", filename="report.pdf")
		payload = _build_payload("sender@example.com", "target@example.com", msg.as_bytes())
		inline, attachment = payload["attachments"]
		self.assertEqual(inline["content_id"], "logo")
		self.assertEqual(inline["disposition"], "inline")
		self.assertEqual(attachment["filename"], "report.pdf")
		self.assertEqual(base64.b64decode(attachment["content"]), b"file bytes")

	def test_empty_body_and_string_message(self) -> None:
		payload = _build_payload("sender@example.com", "target@example.com", "Subject: Empty\n\n")
		self.assertEqual(payload["content"], [{"type": "text/plain", "value": " "}])

	def test_unknown_charset_does_not_break_queue(self) -> None:
		payload = _build_payload(
			"a@example.com", "b@example.com", b'Content-Type: text/plain; charset="unknown"\n\nbody'
		)
		self.assertEqual(payload["content"][0]["value"], "body")


class TestSendgridTransport(unittest.TestCase):
	def setUp(self) -> None:
		self.frappe = MagicMock()
		self.frappe.local.request = None
		self.frappe.throw.side_effect = self.throw
		self.frappe.conf = {}
		self.account = MagicMock(service="Sendgrid HTTP")
		self.account.get_password.return_value = "SG.secret"
		self.queue = MagicMock()
		self.queue.get_email_account.return_value = self.account
		patcher = patch.object(sendgrid, "frappe", self.frappe)
		patcher.start()
		self.addCleanup(patcher.stop)

	@staticmethod
	def throw(message: str) -> None:
		raise ValueError(message)

	def send(self) -> None:
		sendgrid.send_via_sendgrid(self.queue, "a@example.com", "b@example.com", b"Subject: Test\n\nbody")

	@patch.object(sendgrid.requests, "post")
	def test_accepted_and_site_config_fallback(self, post: MagicMock) -> None:
		post.return_value.status_code = 202
		self.account.get_password.return_value = None
		self.frappe.conf = {"sendgrid_api_key": "SG.fallback"}
		self.send()
		self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer SG.fallback")
		self.assertFalse(post.call_args.kwargs["allow_redirects"])
		self.account.get_password.assert_called_once_with("password", raise_exception=False)

	@patch.object(sendgrid.requests, "post")
	def test_missing_key_never_calls_provider(self, post: MagicMock) -> None:
		self.account.get_password.return_value = None
		with self.assertRaisesRegex(ValueError, "not configured"):
			self.send()
		post.assert_not_called()

	@patch.object(sendgrid.requests, "post")
	def test_http_errors_do_not_expose_response(self, post: MagicMock) -> None:
		for status in (301, 400, 401, 429, 500):
			post.return_value.status_code = status
			post.return_value.text = "secret provider body SG.secret"
			with self.assertRaisesRegex(ValueError, f"HTTP {status}") as exc:
				self.send()
			self.assertNotIn("secret", str(exc.exception))

	@patch.object(sendgrid.requests, "post", side_effect=requests.Timeout)
	def test_timeout_is_sanitized(self, post: MagicMock) -> None:
		with self.assertRaisesRegex(ValueError, "could not be reached"):
			self.send()

	@patch.object(sendgrid.requests, "post")
	def test_request_and_other_services_cannot_call_transport(self, post: MagicMock) -> None:
		self.frappe.local.request = object()
		with self.assertRaisesRegex(ValueError, "background"):
			self.send()
		self.frappe.local.request = None
		self.account.service = "Frappe Mail"
		with self.assertRaisesRegex(ValueError, "does not use SendGrid"):
			self.send()
		post.assert_not_called()


class NativeQueue:
	def send(self, *args) -> str:
		self.native_args = args
		return "native"


class FakeQueue(email_queue.SendgridEmailQueueMixin, NativeQueue):
	name = "queue-1"
	sender = "sender@example.com"

	def __init__(self, service: str = "Sendgrid HTTP") -> None:
		self.account = SimpleNamespace(service=service, append_emails_to_sent_folder=False)
		self.get_email_account = MagicMock(return_value=self.account)
		self.can_send_now = MagicMock(return_value=True)
		self.reload = MagicMock()
		self.recipients = [MagicMock(recipient="done@example.com"), MagicMock(recipient="new@example.com")]
		self.recipients[0].is_mail_sent.return_value = True
		self.recipients[1].is_mail_sent.return_value = False


class TestSendgridQueue(unittest.TestCase):
	def setUp(self) -> None:
		self.frappe = MagicMock()
		self.frappe.local.request = None
		self.frappe.in_test = False
		self.context = MagicMock()
		self.context.build_message.return_value = b"message"
		self.patchers = [
			patch.object(email_queue, "frappe", self.frappe),
			patch.object(
				email_queue,
				"SendMailContext",
				return_value=MagicMock(__enter__=MagicMock(return_value=self.context)),
			),
			patch.object(email_queue, "send_via_sendgrid"),
		]
		for patcher in self.patchers:
			patcher.start()
			self.addCleanup(patcher.stop)

	def test_other_transports_use_native_sender(self) -> None:
		for service in ("", "GMail", "Frappe Mail"):
			queue = FakeQueue(service)
			self.assertEqual(queue.send("smtp", "mail", True), "native")
			self.assertEqual(queue.native_args, ("smtp", "mail", True))
		email_queue.send_via_sendgrid.assert_not_called()

	def test_request_queues_after_commit_without_http(self) -> None:
		self.frappe.local.request = object()
		queue = FakeQueue()
		queue.send()
		self.frappe.enqueue.assert_called_once_with(
			"flutter_utils.email.email_queue.send_queued_email",
			name="queue-1",
			force_send=False,
			queue="short",
			enqueue_after_commit=True,
		)
		email_queue.send_via_sendgrid.assert_not_called()
		queue.reload.assert_not_called()

	def test_guest_otp_worker_does_not_use_desk_permission_endpoint(self) -> None:
		email_queue.send_queued_email("queue-1")
		self.frappe.get_doc.assert_called_once_with("Email Queue", "queue-1")
		self.frappe.get_doc.return_value.send.assert_called_once_with(force_send=False)
		self.frappe.get_doc.return_value.check_permission.assert_not_called()

	def test_worker_skips_sent_recipients_and_updates_progress(self) -> None:
		queue = FakeQueue()
		queue.send()
		queue.reload.assert_called_once()
		email_queue.send_via_sendgrid.assert_called_once_with(
			queue, queue.sender, "new@example.com", b"message"
		)
		self.context.update_recipient_status_to_sent.assert_called_once_with(queue.recipients[1])

	def test_failure_does_not_mark_recipient_sent(self) -> None:
		email_queue.send_via_sendgrid.side_effect = ValueError("Rejected")
		# The real SendMailContext persists error/retry state on __exit__.
		with self.assertRaises(ValueError):
			FakeQueue().send()
		self.context.update_recipient_status_to_sent.assert_not_called()

	def test_test_mode_never_sends_real_email(self) -> None:
		self.frappe.in_test = True
		self.frappe.flags.testing_email = False
		FakeQueue().send()
		email_queue.send_via_sendgrid.assert_not_called()
		self.assertEqual(self.frappe.flags.sent_mail, b"message")

	def test_stale_duplicate_job_does_not_resend(self) -> None:
		queue = FakeQueue()
		queue.can_send_now.side_effect = [True, False]
		queue.send()
		email_queue.send_via_sendgrid.assert_not_called()


class TestSendgridSetup(unittest.TestCase):
	@patch.object(setup, "frappe")
	def test_service_setup_is_idempotent_and_preserves_options(self, frappe: MagicMock) -> None:
		field = frappe.get_meta.return_value.get_field.return_value
		field.options = "\nGMail\nCustom Provider"
		setup.ensure_sendgrid_service()
		setter = frappe.make_property_setter.call_args.args[0]
		self.assertEqual(setter["value"], "\nGMail\nCustom Provider\nSendgrid HTTP")
		field.options = setter["value"]
		setup.ensure_sendgrid_service()
		frappe.make_property_setter.assert_called_once()
		frappe.db.commit.assert_not_called()

	def test_legacy_hooks_do_not_compete(self) -> None:
		if importlib.util.find_spec("frappe_sendgrid") is None:
			self.skipTest("Deprecated compatibility app is not installed")
		from frappe_sendgrid import hooks
		from frappe_sendgrid.utils.email_sender import send_via_sendgrid

		for key in ("override_email_send", "override_doctype_class", "doctype_js"):
			self.assertFalse(getattr(hooks, key, None))
		self.assertIs(send_via_sendgrid, sendgrid.send_via_sendgrid)

	def test_sendgrid_account_skips_smtp_validation(self) -> None:
		class Base:
			validate = MagicMock()
			validate_smtp_conn = MagicMock()
			get_smtp_server = MagicMock(return_value="smtp")

		class Account(email_account.SendgridEmailAccountMixin, Base):
			service = "Sendgrid HTTP"

		account = Account()
		account.validate()
		account.validate_smtp_conn()
		self.assertIsNone(account.get_smtp_server())
		self.assertEqual(account.enable_incoming, 0)
		self.assertEqual(account.always_use_account_email_id_as_sender, 1)
		Base.validate_smtp_conn.assert_not_called()
		account.service = "GMail"
		account.validate_smtp_conn()
		self.assertEqual(account.get_smtp_server(), "smtp")
		Base.validate_smtp_conn.assert_called_once()


class TestNativeQueueLifecycle(unittest.TestCase):
	"""Exercise real Frappe context transitions without connecting to a database."""

	def queue(self) -> MagicMock:
		queue = MagicMock()
		queue.retry = 0
		queue.recipients = []
		queue.redact_message_after_send = False
		return queue

	def test_success_updates_sent_and_redacts(self) -> None:
		queue = self.queue()
		queue.redact_message_after_send = True
		with SendMailContext(queue) as context:
			recipient = MagicMock()
			context.update_recipient_status_to_sent(recipient)
		recipient.update_db.assert_called_once_with(status="Sent", commit=True)
		self.assertEqual(queue.update_status.call_args_list[-1].kwargs, {"status": "Sent", "commit": True})
		queue.redact_message.assert_called_once()

	def test_failure_keeps_partial_progress_and_retries(self) -> None:
		queue = self.queue()
		with (
			patch("frappe.email.doctype.email_queue.email_queue.get_email_retry_limit", return_value=3),
			patch(
				"frappe.email.doctype.email_queue.email_queue.frappe.get_traceback", return_value="safe error"
			),
			self.assertRaises(ValueError),
			SendMailContext(queue) as context,
		):
			context.update_recipient_status_to_sent(MagicMock())
			raise ValueError("provider rejected")
		self.assertEqual(
			queue.update_status.call_args_list[-1].kwargs,
			{"error": "safe error", "status": "Partially Sent", "retry": 1, "commit": True},
		)
		queue.redact_message.assert_not_called()

	def test_retry_exhaustion_uses_native_failure_notification(self) -> None:
		queue = self.queue()
		queue.retry = 3
		with (
			patch("frappe.email.doctype.email_queue.email_queue.get_email_retry_limit", return_value=3),
			patch(
				"frappe.email.doctype.email_queue.email_queue.frappe.get_traceback", return_value="safe error"
			),
			patch.object(SendMailContext, "notify_failed_email") as notify,
			self.assertRaises(ValueError),
			SendMailContext(queue),
		):
			raise ValueError("provider rejected")
		notify.assert_called_once()
		self.assertEqual(queue.update_status.call_args_list[-1].kwargs["status"], "Error")


if __name__ == "__main__":
	unittest.main()

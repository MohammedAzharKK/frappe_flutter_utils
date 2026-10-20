import base64
from email import message_from_bytes, message_from_string
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parseaddr
from typing import Any


def _decode_header_value(value: str | None) -> str:
	return str(make_header(decode_header(value))) if value else ""


def _address(value: str) -> dict[str, str]:
	name, address = parseaddr(_decode_header_value(value))
	result = {"email": address}
	if name:
		result["name"] = name
	return result


def _decode_part(part: Message) -> str:
	payload = part.get_payload(decode=True)
	if payload is None:
		return ""
	try:
		return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
	except LookupError:
		return payload.decode("utf-8", errors="replace")


def _build_attachment(part: Message) -> dict[str, str]:
	attachment = {
		"content": base64.b64encode(part.get_payload(decode=True) or b"").decode(),
		"type": part.get_content_type(),
		"filename": _decode_header_value(part.get_filename() or "attachment"),
		"disposition": part.get_content_disposition() or "attachment",
	}
	if content_id := part.get("Content-ID"):
		attachment["content_id"] = content_id.strip("<>")
	return attachment


def _build_payload(sender: str, recipient: str, message: bytes | str) -> dict[str, Any]:
	msg = message_from_bytes(message) if isinstance(message, bytes) else message_from_string(message)
	bodies: dict[str, list[str]] = {"text/plain": [], "text/html": []}
	attachments = []
	for part in msg.walk():
		if part.is_multipart():
			continue
		if part.get_content_disposition() in ("attachment", "inline") or part.get_filename():
			attachments.append(_build_attachment(part))
		elif part.get_content_type() in bodies:
			bodies[part.get_content_type()].append(_decode_part(part))
	content = [{"type": kind, "value": "\n".join(parts)} for kind, parts in bodies.items() if any(parts)]
	payload = {
		"personalizations": [{"to": [_address(recipient)]}],
		"from": _address(sender),
		"subject": _decode_header_value(msg.get("Subject")),
		"content": content or [{"type": "text/plain", "value": " "}],
	}
	if reply_to := msg.get("Reply-To"):
		payload["reply_to"] = _address(reply_to)
	if attachments:
		payload["attachments"] = attachments
	return payload

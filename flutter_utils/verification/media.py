"""Bounded ISO-BMFF and inert-image validation, not video decoding or automated identity analysis."""

from __future__ import annotations

import frappe
from frappe import _

# Every inert document/image/video format the shared engine can store. A
# document type still narrows this per app; this set is the outer boundary.
SUPPORTED_EXTENSIONS = {
	"pdf",
	"png",
	"jpg",
	"jpeg",
	"webp",
	"gif",
	"bmp",
	"mp4",
	"m4v",
	"mov",
	"3gp",
	"3g2",
}

VIDEO_EXTENSIONS = {"mp4", "m4v", "mov", "3gp", "3g2"}
VIDEO_CODECS = {b"avc1", b"avc3", b"hvc1", b"hev1", b"mp4v", b"vp09", b"av01"}
MP4_BRANDS = {
	b"isom",
	b"iso2",
	b"iso3",
	b"iso4",
	b"iso5",
	b"iso6",
	b"mp41",
	b"mp42",
	b"avc1",
	b"M4V ",
	b"M4VH",
	b"M4VP",
	b"dash",
	# QuickTime and 3GPP/3GPP2 phone containers share the ISO-BMFF layout.
	b"qt  ",
	b"3gp4",
	b"3gp5",
	b"3gp6",
	b"3gp7",
	b"3g2a",
	b"3g2b",
	b"3g2c",
	b"3ge6",
	b"3ge7",
	b"MSNV",
	b"mmp4",
	b"F4V ",
}


def _fail(label: str, message: str) -> None:
	"""Raise a validation error prefixed with the failing document, when known."""
	frappe.throw(f"{label}: {message}" if label else message)


def matches_signature(extension: str, content: bytes) -> bool:
	"""True when [content] begins with the known signature for [extension].

	Only these inert signatures are trusted; a filename or client MIME type is
	never enough on its own.
	"""
	if extension == "pdf":
		return content.startswith(b"%PDF-")
	if extension == "png":
		return content.startswith(b"\x89PNG\r\n\x1a\n")
	if extension in {"jpg", "jpeg"}:
		return content.startswith(b"\xff\xd8\xff")
	if extension == "gif":
		return content.startswith((b"GIF87a", b"GIF89a"))
	if extension == "bmp":
		return content.startswith(b"BM")
	if extension == "webp":
		return len(content) >= 12 and content.startswith(b"RIFF") and content[8:12] == b"WEBP"
	return False


def _boxes(data: memoryview, label: str = "") -> list[tuple[bytes, memoryview]]:
	result = []
	offset = 0
	while offset < len(data):
		if len(data) - offset < 8 or len(result) >= 10000:
			_fail(label, _("Invalid video container."))
		size = int.from_bytes(data[offset : offset + 4], "big")
		kind = bytes(data[offset + 4 : offset + 8])
		header = 8
		if size == 1:
			if len(data) - offset < 16:
				_fail(label, _("Invalid video container."))
			size = int.from_bytes(data[offset + 8 : offset + 16], "big")
			header = 16
		elif size == 0:
			size = len(data) - offset
		if size < header or size > len(data) - offset:
			_fail(label, _("Invalid video container."))
		result.append((kind, data[offset + header : offset + size]))
		offset += size
	return result


def _children(boxes: list[tuple[bytes, memoryview]], kind: bytes) -> list[memoryview]:
	return [payload for name, payload in boxes if name == kind]


def validate_mp4(content: bytes, label: str = "") -> None:
	"""Validate any ISO-BMFF video (MP4, M4V, MOV, 3GP, 3G2) container."""
	boxes = _boxes(memoryview(content), label)
	brands = _children(boxes, b"ftyp")
	if len(brands) != 1 or len(brands[0]) < 8 or (len(brands[0]) - 8) % 4:
		_fail(label, _("This is not a supported video file."))
	brand = brands[0]
	declared = {bytes(brand[:4]), *(bytes(brand[index : index + 4]) for index in range(8, len(brand), 4))}
	if not declared.intersection(MP4_BRANDS) or not any(len(data) for data in _children(boxes, b"mdat")):
		_fail(label, _("This is not a supported video file."))
	for movie in _children(boxes, b"moov"):
		for track in _children(_boxes(movie, label), b"trak"):
			for media in _children(_boxes(track, label), b"mdia"):
				children = _boxes(media, label)
				if not any(
					len(handler) >= 12 and bytes(handler[8:12]) == b"vide"
					for handler in _children(children, b"hdlr")
				):
					continue
				for info in _children(children, b"minf"):
					for table in _children(_boxes(info, label), b"stbl"):
						for description in _children(_boxes(table, label), b"stsd"):
							if len(description) < 8:
								continue
							entries = _boxes(description[8:], label)
							if int.from_bytes(description[4:8], "big") != len(entries):
								_fail(label, _("Invalid video track."))
							for codec, entry in entries:
								if (
									codec in VIDEO_CODECS
									and len(entry) >= 78
									and int.from_bytes(entry[24:26], "big")
									and int.from_bytes(entry[26:28], "big")
								):
									return
	_fail(label, _("The video must contain a supported video track, not audio only."))

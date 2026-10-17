from __future__ import annotations

from contextlib import ExitStack
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import frappe

from flutter_utils.verification import evidence, media


def box(kind: bytes, payload: bytes) -> bytes:
	return (len(payload) + 8).to_bytes(4, "big") + kind + payload


def video_sample(handler: bytes = b"vide", codec: bytes = b"avc1", brand: bytes = b"isom") -> bytes:
	entry = bytearray(78)
	entry[24:26] = (16).to_bytes(2, "big")
	entry[26:28] = (16).to_bytes(2, "big")
	description = box(b"stsd", b"\0" * 4 + (1).to_bytes(4, "big") + box(codec, bytes(entry)))
	track = box(
		b"trak",
		box(
			b"mdia", box(b"hdlr", b"\0" * 8 + handler + b"\0" * 12) + box(b"minf", box(b"stbl", description))
		),
	)
	return box(b"ftyp", brand + b"\0\0\0\0mp42") + box(b"moov", track) + box(b"mdat", b"sample")


def throw(message: str, exc: type[Exception] = frappe.ValidationError, **kwargs: object) -> None:
	raise exc(message)


class VerificationMediaTests(TestCase):
	def setUp(self) -> None:
		self.stack = ExitStack()
		self.addCleanup(self.stack.close)
		for module in (media, evidence):
			self.stack.enter_context(patch.object(module, "_", lambda message: message))
		self.stack.enter_context(patch("frappe.throw", throw))
		self.stack.enter_context(patch.object(evidence, "get_max_file_size", return_value=1024 * 1024))

	def test_mp4_with_video_track_is_supported(self) -> None:
		media.validate_mp4(video_sample())
		config = frappe._dict(allowed_extensions="mp4", max_file_size_mb=10)
		evidence.check_content("intro.mp4", video_sample(), config)

	def test_audio_only_unknown_codec_and_truncated_video_rejected(self) -> None:
		for data in (
			video_sample(b"soun"),
			video_sample(codec=b"xxxx"),
			video_sample()[:-1],
			b"not a video",
			box(b"ftyp", b"isom\0\0\0\0mp42"),
		):
			with self.subTest(data=data[:16]), self.assertRaises(frappe.ValidationError):
				media.validate_mp4(data)

	def test_mp4_does_not_bypass_catalog_or_global_limit(self) -> None:
		with self.assertRaises(frappe.ValidationError):
			evidence.check_content(
				"intro.mp4", video_sample(), frappe._dict(allowed_extensions="png", max_file_size_mb=10)
			)
		with (
			patch.object(evidence, "get_max_file_size", return_value=10),
			self.assertRaises(frappe.ValidationError),
		):
			evidence.check_content(
				"intro.mp4", video_sample(), frappe._dict(allowed_extensions="mp4", max_file_size_mb=10)
			)

	def test_widened_image_and_document_formats_supported(self) -> None:
		samples = {
			"png": b"\x89PNG\r\n\x1a\n" + b"x",
			"jpg": b"\xff\xd8\xff\xe0" + b"x",
			"jpeg": b"\xff\xd8\xff\xe0" + b"x",
			"webp": b"RIFF" + (4).to_bytes(4, "little") + b"WEBP" + b"x",
			"gif": b"GIF89a" + b"x",
			"bmp": b"BM" + b"x",
			"pdf": b"%PDF-1.4\n%%EOF",
		}
		for extension, content in samples.items():
			config = frappe._dict(allowed_extensions=extension, max_file_size_mb=10)
			with self.subTest(extension=extension):
				evidence.check_content(f"id.{extension}", content, config)

	def test_iso_bmff_container_variants_supported(self) -> None:
		for extension, brand in (
			("mp4", b"isom"),
			("m4v", b"M4V "),
			("mov", b"qt  "),
			("3gp", b"3gp4"),
			("3g2", b"3g2a"),
		):
			config = frappe._dict(allowed_extensions=extension, max_file_size_mb=10)
			with self.subTest(extension=extension):
				evidence.check_content(f"intro.{extension}", video_sample(brand=brand), config)

	def test_mislabeled_file_raises_document_named_error(self) -> None:
		config = frappe._dict(allowed_extensions="jpg\npng\nwebp", max_file_size_mb=10)
		webp = b"RIFF" + (4).to_bytes(4, "little") + b"WEBP" + b"x"
		with self.assertRaises(frappe.ValidationError) as caught:
			evidence.check_content("selfie.jpg", webp, config, "Listener Profile Photo")
		self.assertIn("Listener Profile Photo", str(caught.exception))
		with self.assertRaises(frappe.ValidationError):
			evidence.check_content("id.txt", b"hello", config, "Listener Identity")

	def test_private_import_rejects_foreign_public_and_attached_files_before_open(self) -> None:
		for values in ({"owner": "other"}, {"is_private": 0}, {"attached_to_name": "already-attached"}):
			file = frappe._dict(
				owner="owner", is_private=1, attached_to_name=None, file_url="/private/files/id.png", **{}
			)
			file.update(values)
			with (
				patch("frappe.session", frappe._dict(user="owner")),
				patch("frappe.get_doc", return_value=file),
			):
				with self.assertRaises(frappe.PermissionError), evidence.owned_private_upload("file"):
					self.fail("Untrusted files must not be opened")

	def test_private_import_streams_binary_without_reparenting(self) -> None:
		from unittest.mock import mock_open

		file = SimpleNamespace(
			owner="owner",
			is_private=1,
			attached_to_name=None,
			file_url="/private/files/id.png",
			file_name="id.png",
			validate_file_path=lambda: None,
			get_full_path=lambda: "/private/mock",
		)
		with (
			patch("frappe.session", frappe._dict(user="owner")),
			patch("frappe.get_doc", return_value=file),
			patch("builtins.open", mock_open(read_data=b"binary")) as opened,
		):
			with evidence.owned_private_upload("file") as upload:
				self.assertEqual(upload.stream.read(), b"binary")
			opened.assert_called_once_with("/private/mock", "rb")
		self.assertIsNone(file.attached_to_name)

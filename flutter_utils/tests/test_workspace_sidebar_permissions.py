from __future__ import annotations

from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import frappe

from flutter_utils.workspace_sidebar import _get_permitted_sidebar_items, get_default_workspace_sidebar


class TestWorkspaceSidebarPermissions(TestCase):
	def test_native_permission_helper_excludes_unavailable_workspaces(self) -> None:
		sidebar = SimpleNamespace(
			items=[
				frappe._dict(type="Link", label="Home", link_to="Home", link_type="Workspace"),
				frappe._dict(type="Link", label="Secret", link_to="Secret", link_type="Workspace"),
			]
		)
		with patch("flutter_utils.workspace_sidebar.DeskViews", return_value=SimpleNamespace()):
			result = _get_permitted_sidebar_items(sidebar, {"Home"})
		self.assertEqual([item["label"] for item in result], ["Home"])

	def test_filters_links_without_legacy_sidebar_method(self) -> None:
		# Archived WorkspaceSidebar documents no longer implement is_item_allowed.
		sidebar = SimpleNamespace(
			items=[
				frappe._dict(type="Section Break", label="Hidden section"),
				frappe._dict(type="Link", label="Secret", link_to="Secret", link_type="DocType", child=1),
				frappe._dict(type="Section Break", label="Visible section"),
				frappe._dict(type="Link", label="Home", link_to="Home", link_type="Workspace", child=1),
			]
		)
		allowed_workspaces = {"Home"}
		with (
			patch("flutter_utils.workspace_sidebar.DeskViews") as context_class,
			patch("flutter_utils.workspace_sidebar.is_item_allowed", side_effect=[False, True]) as allowed,
		):
			result = _get_permitted_sidebar_items(sidebar, allowed_workspaces)

		self.assertEqual([item["label"] for item in result], ["Visible section", "Home"])
		self.assertEqual(context_class.return_value.allowed_workspaces, allowed_workspaces)
		self.assertEqual(allowed.call_count, 2)
		allowed.assert_any_call("Secret", "DocType", context_class.return_value)
		allowed.assert_any_call("Home", "Workspace", context_class.return_value)

	def test_returns_no_items_when_all_links_are_denied(self) -> None:
		sidebar = SimpleNamespace(
			items=[
				frappe._dict(type="Section Break", label="Restricted"),
				frappe._dict(type="Link", link_to="Secret", link_type="DocType", child=1),
			]
		)
		with (
			patch("flutter_utils.workspace_sidebar.DeskViews"),
			patch("flutter_utils.workspace_sidebar.is_item_allowed", return_value=False),
		):
			self.assertEqual(_get_permitted_sidebar_items(sidebar, set()), [])

	def test_guest_does_not_load_sidebar(self) -> None:
		with patch("flutter_utils.workspace_sidebar.get_sidebar_for_default_workspace") as resolve:
			self.assertIsNone(get_default_workspace_sidebar("Guest"))
		resolve.assert_not_called()

	def test_missing_sidebar_returns_none(self) -> None:
		with patch("flutter_utils.workspace_sidebar.get_sidebar_for_default_workspace", return_value=None):
			self.assertIsNone(get_default_workspace_sidebar("test@example.com"))

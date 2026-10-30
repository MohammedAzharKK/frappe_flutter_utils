from __future__ import annotations

from typing import Any

import frappe
from frappe.desk.desk_views import DeskViews
from frappe.desk.desktop import get_workspaces
try:
	from frappe.desk.utils import is_item_allowed
except ImportError:
	# Frappe 16 versions before the helper moved into desk.utils.
	def is_item_allowed(name: str, item_type: str, context: Any) -> bool:
		return bool(DeskViews.is_item_allowed(context, name, item_type, context.allowed_workspaces))


def get_default_workspace_sidebar(user: str | None = None) -> dict[str, Any] | None:
	"""Return the sidebar linked to the user's native Default Workspace."""
	user = user or frappe.session.user
	if not user or user == "Guest":
		return None

	sidebar = get_sidebar_for_default_workspace(user)
	if not sidebar:
		return None

	allowed_workspaces = {workspace.name for workspace in get_workspaces()["pages"]}
	items = _get_permitted_sidebar_items(sidebar, allowed_workspaces)
	return {
		"name": sidebar.name,
		"title": sidebar.title,
		"header_icon": sidebar.header_icon,
		"items": items,
	}


def get_sidebar_for_default_workspace(user: str | None = None) -> Any | None:
	"""Resolve a user's Default Workspace to its uniquely linked sidebar."""
	user = user or frappe.session.user
	if not user or user == "Guest":
		return None

	workspace_name = frappe.db.get_value("User", user, "default_workspace")
	if not workspace_name:
		return None

	allowed_workspaces = {workspace.name for workspace in get_workspaces()["pages"]}
	if workspace_name not in allowed_workspaces:
		return None

	sidebar_names = frappe.get_all(
		"Workspace Sidebar Item",
		filters={
			"parenttype": "Workspace Sidebar",
			"type": "Link",
			"link_type": "Workspace",
			"link_to": workspace_name,
		},
		order_by="idx asc, parent asc",
		pluck="parent",
	)
	unique_sidebar_names = list(dict.fromkeys(sidebar_names))
	if workspace_name in unique_sidebar_names:
		sidebar_name = workspace_name
	elif len(unique_sidebar_names) == 1:
		sidebar_name = unique_sidebar_names[0]
	else:
		return None

	if not frappe.db.exists("Workspace Sidebar", sidebar_name):
		return None
	return frappe.get_doc("Workspace Sidebar", sidebar_name)


def _get_permitted_sidebar_items(sidebar: Any, allowed_workspaces: set[str]) -> list[dict[str, Any]]:
	"""Keep section breaks only when they contain at least one permitted child link."""
	items: list[dict[str, Any]] = []
	permission_context = DeskViews()
	permission_context.allowed_workspaces = allowed_workspaces
	pending_section: dict[str, Any] | None = None
	for item in sidebar.items:
		if item.type == "Section Break":
			pending_section = _serialize_sidebar_item(item)
			continue

		if item.type != "Link" or not item.link_type or not is_item_allowed(item.link_to, item.link_type, permission_context):
			continue

		if pending_section and item.child:
			items.append(pending_section)
			pending_section = None
		items.append(_serialize_sidebar_item(item))

	return items


def _serialize_sidebar_item(item: Any) -> dict[str, Any]:
	return {
		"type": item.type,
		"label": item.label,
		"icon": item.icon,
		"link_type": item.link_type,
		"link_to": item.link_to,
		"url": item.url,
		"child": item.child,
		"indent": item.indent,
		"collapsible": item.collapsible,
		"keep_closed": item.keep_closed,
	}

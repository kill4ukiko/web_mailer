"""Contact-list management endpoints (reusable groups of contacts)."""
from __future__ import annotations

from flask import Blueprint, jsonify, request
from pydantic import ValidationError

from app.core.supabase_client import SupabaseClientError, get_supabase
from app.schemas.lists import ListCreate, ListUpdate, MembersAdd

bp = Blueprint("lists", __name__, url_prefix="/api/lists")


@bp.get("")
def list_lists():
    """GET /api/lists — all lists with member counts."""
    return jsonify({"items": get_supabase().list_lists()}), 200


@bp.post("")
def create_list():
    """POST /api/lists  {name, description?, emails?, contact_ids?}"""
    try:
        payload = ListCreate.model_validate(request.get_json(silent=True) or {})
    except ValidationError as exc:
        return jsonify({"error": exc.errors(include_url=False)}), 422

    supabase = get_supabase()
    try:
        entry = supabase.create_list(payload.name.strip(), payload.description)
    except SupabaseClientError as exc:
        return jsonify({"error": str(exc)}), 409
    except Exception as exc:  # noqa: BLE001 - unique-name violation
        return jsonify({"error": f"could not create list: {exc}"}), 409

    list_id = entry["id"]
    added = supabase.add_members(list_id, payload.contact_ids)
    if payload.emails:
        added += supabase.add_members_by_email(list_id, [str(e) for e in payload.emails])
    entry["member_count"] = added
    return jsonify({"list": entry}), 201


@bp.get("/<list_id>")
def get_list(list_id: str):
    """GET /api/lists/<id> — list metadata + members."""
    supabase = get_supabase()
    entry = supabase.get_list(list_id)
    if entry is None:
        return jsonify({"error": "list not found"}), 404
    entry["members"] = supabase.list_members(list_id)
    return jsonify(entry), 200


@bp.patch("/<list_id>")
def update_list(list_id: str):
    """PATCH /api/lists/<id> — rename / re-describe a list."""
    try:
        payload = ListUpdate.model_validate(request.get_json(silent=True) or {})
    except ValidationError as exc:
        return jsonify({"error": exc.errors(include_url=False)}), 422

    data = payload.model_dump(exclude_none=True)
    if not data:
        return jsonify({"error": "no fields to update"}), 400

    supabase = get_supabase()
    if supabase.get_list(list_id) is None:
        return jsonify({"error": "list not found"}), 404
    updated = supabase.update_list(list_id, data)
    return jsonify({"list": updated}), 200


@bp.delete("/<list_id>")
def delete_list(list_id: str):
    """DELETE /api/lists/<id> — remove a list (memberships cascade)."""
    supabase = get_supabase()
    if supabase.get_list(list_id) is None:
        return jsonify({"error": "list not found"}), 404
    supabase.delete_list(list_id)
    return jsonify({"deleted": list_id}), 200


@bp.post("/<list_id>/members")
def add_members(list_id: str):
    """POST /api/lists/<id>/members  {emails?, contact_ids?}"""
    try:
        payload = MembersAdd.model_validate(request.get_json(silent=True) or {})
    except ValidationError as exc:
        return jsonify({"error": exc.errors(include_url=False)}), 422

    supabase = get_supabase()
    if supabase.get_list(list_id) is None:
        return jsonify({"error": "list not found"}), 404

    added = supabase.add_members(list_id, payload.contact_ids)
    if payload.emails:
        added += supabase.add_members_by_email(list_id, [str(e) for e in payload.emails])
    return jsonify({"added": added, "members": supabase.list_members(list_id)}), 200


@bp.delete("/<list_id>/members/<contact_id>")
def remove_member(list_id: str, contact_id: str):
    """DELETE /api/lists/<id>/members/<contact_id> — drop one member."""
    removed = get_supabase().remove_member(list_id, contact_id)
    if not removed:
        return jsonify({"error": "membership not found"}), 404
    return jsonify({"removed": contact_id}), 200

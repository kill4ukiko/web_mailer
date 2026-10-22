"""SMS list management endpoints (reusable phone groups)."""
from __future__ import annotations

from flask import Blueprint, jsonify, request
from pydantic import ValidationError

from app.core.bird_sms_content import normalise_phone
from app.core.bird_sms_config import effective_bird_settings
from app.core.bird_sms_store import get_bird_store
from app.core.supabase_client import SupabaseClientError
from app.schemas.bird_sms_lists import BirdListCreate, BirdListUpdate, BirdMembersAdd

bp = Blueprint("bird_sms_lists", __name__, url_prefix="/api/bird-sms/lists")


def _country_code() -> str | None:
    try:
        return effective_bird_settings(get_bird_store()).get("default_country_code")
    except Exception:  # noqa: BLE001
        return None


def _normalise_phones(phones: list[str]) -> tuple[list[str], list[str]]:
    code = _country_code()
    valid: list[str] = []
    invalid: list[str] = []
    for raw in phones:
        phone = normalise_phone(raw, code)
        if phone is None:
            invalid.append(raw)
        else:
            valid.append(phone)
    return valid, invalid


@bp.get("")
def list_lists():
    """GET /api/sms/lists — all SMS lists with member counts."""
    return jsonify({"items": get_bird_store().list_sms_lists()}), 200


@bp.post("")
def create_list():
    """POST /api/sms/lists  {name, description?, phones?, contact_ids?}"""
    try:
        payload = BirdListCreate.model_validate(request.get_json(silent=True) or {})
    except ValidationError as exc:
        return jsonify({"error": exc.errors(include_url=False)}), 422

    supabase = get_bird_store()
    try:
        entry = supabase.create_sms_list(payload.name.strip(), payload.description)
    except SupabaseClientError as exc:
        return jsonify({"error": str(exc)}), 409
    except Exception as exc:  # noqa: BLE001 - unique-name violation
        return jsonify({"error": f"could not create list: {exc}"}), 409

    list_id = entry["id"]
    added = supabase.add_sms_members(list_id, payload.contact_ids)
    phones, invalid = _normalise_phones(payload.phones)
    if phones:
        added += supabase.add_sms_members_by_phone(list_id, phones)
    entry["member_count"] = added
    return jsonify({"list": entry, "invalid": invalid[:100]}), 201


@bp.get("/<list_id>")
def get_list(list_id: str):
    """GET /api/sms/lists/<id> — list metadata + members."""
    supabase = get_bird_store()
    entry = supabase.get_sms_list(list_id)
    if entry is None:
        return jsonify({"error": "list not found"}), 404
    entry["members"] = supabase.list_sms_members(list_id)
    return jsonify(entry), 200


@bp.patch("/<list_id>")
def update_list(list_id: str):
    """PATCH /api/sms/lists/<id> — rename / re-describe a list."""
    try:
        payload = BirdListUpdate.model_validate(request.get_json(silent=True) or {})
    except ValidationError as exc:
        return jsonify({"error": exc.errors(include_url=False)}), 422

    data = payload.model_dump(exclude_none=True)
    if not data:
        return jsonify({"error": "no fields to update"}), 400

    supabase = get_bird_store()
    if supabase.get_sms_list(list_id) is None:
        return jsonify({"error": "list not found"}), 404
    return jsonify({"list": supabase.update_sms_list(list_id, data)}), 200


@bp.delete("/<list_id>")
def delete_list(list_id: str):
    """DELETE /api/sms/lists/<id> — remove a list (memberships cascade)."""
    supabase = get_bird_store()
    if supabase.get_sms_list(list_id) is None:
        return jsonify({"error": "list not found"}), 404
    supabase.delete_sms_list(list_id)
    return jsonify({"deleted": list_id}), 200


@bp.post("/<list_id>/members")
def add_members(list_id: str):
    """POST /api/sms/lists/<id>/members  {phones?, contact_ids?}"""
    try:
        payload = BirdMembersAdd.model_validate(request.get_json(silent=True) or {})
    except ValidationError as exc:
        return jsonify({"error": exc.errors(include_url=False)}), 422

    supabase = get_bird_store()
    if supabase.get_sms_list(list_id) is None:
        return jsonify({"error": "list not found"}), 404

    added = supabase.add_sms_members(list_id, payload.contact_ids)
    phones, invalid = _normalise_phones(payload.phones)
    if phones:
        added += supabase.add_sms_members_by_phone(list_id, phones)
    return jsonify({
        "added": added,
        "invalid": invalid[:100],
        "members": supabase.list_sms_members(list_id),
    }), 200


@bp.delete("/<list_id>/members/<contact_id>")
def remove_member(list_id: str, contact_id: str):
    """DELETE /api/sms/lists/<id>/members/<contact_id> — drop one member."""
    removed = get_bird_store().remove_sms_member(list_id, contact_id)
    if not removed:
        return jsonify({"error": "membership not found"}), 404
    return jsonify({"removed": contact_id}), 200

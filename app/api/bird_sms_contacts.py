"""SMS contact management & import endpoints.

Mirrors the email contacts API but is phone-keyed and completely isolated: an
SMS import can never touch email contacts, and phone suppression is preserved by
re-import exactly like email suppression is.
"""
from __future__ import annotations

import io
import json

import pandas as pd
from flask import Blueprint, jsonify, request
from pydantic import ValidationError

from app.core.bird_sms_config import effective_bird_settings
from app.core.bird_sms_store import get_bird_store
from app.core.supabase_client import SMS_DELIVERY_FILTERS
from app.extensions import limiter
from app.schemas.bird_sms_contacts import (
    BirdBulkDelete,
    BirdContactCreate,
    BirdContactStatusUpdate,
    BirdContactUpdate,
    BirdImportCommit,
)

bp = Blueprint("bird_sms_contacts", __name__, url_prefix="/api/bird-sms/contacts")

# Cap on rows returned for client-side staging (keeps the payload sane).
_MAX_STAGED_ROWS = 5000

# Header aliases used to auto-suggest a column mapping.
_ALIASES = {
    "phone": [
        "phone", "phone_number", "phonenumber", "mobile", "mobile_number",
        "number", "tel", "telephone", "msisdn", "cell", "whatsapp",
    ],
    "first_name": ["first_name", "firstname", "first name", "given name", "given_name", "first"],
    "last_name": ["last_name", "lastname", "last name", "surname", "family name", "family_name", "last"],
    "country": ["country", "country_code", "cc", "nation"],
}


def _load_dataframe(raw: bytes, filename: str) -> pd.DataFrame:
    # Read everything as text: pandas would otherwise coerce phone columns to
    # ints/floats and silently drop the leading "+" (and a national leading 0).
    if filename.endswith(".xlsx") or filename.endswith(".xls"):
        return pd.read_excel(io.BytesIO(raw), dtype=str)
    return pd.read_csv(io.BytesIO(raw), dtype=str)


def _suggest_mapping(columns: list[str]) -> dict[str, str]:
    lowered = {c.lower().strip(): c for c in columns}
    suggested: dict[str, str] = {}
    for field, aliases in _ALIASES.items():
        for alias in aliases:
            if alias in lowered:
                suggested[field] = lowered[alias]
                break
    return suggested


def _clean(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    text = str(value).strip()
    return text or None


def _clean_phone_value(value) -> str | None:
    """CSV/Excel stores big numbers as floats (e.g. 15551234567.0)."""
    if isinstance(value, float) and not pd.isna(value) and value.is_integer():
        return str(int(value))
    if isinstance(value, int):
        return str(value)
    return _clean(value)


def _default_country_code() -> str | None:
    try:
        return effective_bird_settings(get_bird_store()).get("default_country_code")
    except Exception:  # noqa: BLE001
        return None


@bp.get("")
def list_contacts():
    """GET /api/sms/contacts?page&limit&status&delivery&deleted=1"""
    try:
        page = max(int(request.args.get("page", 1)), 1)
        limit = min(max(int(request.args.get("limit", 50)), 1), 500)
    except (TypeError, ValueError):
        return jsonify({"error": "page/limit must be integers"}), 400

    status = request.args.get("status")
    delivery = request.args.get("delivery")
    if delivery and delivery not in SMS_DELIVERY_FILTERS:
        return jsonify({
            "error": f"unknown delivery filter: {delivery}",
            "allowed": sorted(SMS_DELIVERY_FILTERS),
        }), 400
    include_deleted = request.args.get("deleted") in ("1", "true", "yes")
    data = get_bird_store().list_sms_contacts(
        page=page, limit=limit, status=status,
        include_deleted=include_deleted, delivery=delivery,
    )
    return jsonify(data), 200


@bp.post("")
def create_contact():
    """POST /api/sms/contacts  {phone, first_name?, last_name?, metadata?}"""
    from app.core.bird_sms_content import normalise_phone

    try:
        payload = BirdContactCreate.model_validate(request.get_json(silent=True) or {})
    except ValidationError as exc:
        return jsonify({"error": exc.errors(include_url=False)}), 422

    phone = normalise_phone(payload.phone, _default_country_code())
    if phone is None:
        return jsonify({
            "error": "invalid phone number; use international format (e.g. +15551234567) "
                     "or configure a default country code in SMS settings",
        }), 422

    data = payload.model_dump(exclude_none=True)
    data["phone"] = phone
    rows = get_bird_store().create_sms_contact(data)
    row = rows[0] if rows else None
    return jsonify({"created": row}), 201 if row else 409


@bp.post("/import/preview")
@limiter.limit("30 per minute")
def preview_import():
    """POST /api/sms/contacts/import/preview  (multipart, field: file)"""
    uploaded = request.files.get("file")
    if uploaded is None or uploaded.filename == "":
        return jsonify({"error": "a file upload is required (field: 'file')"}), 400

    try:
        df = _load_dataframe(uploaded.read(), uploaded.filename.lower())
    except Exception as exc:
        return jsonify({"error": f"unable to parse file: {exc}"}), 400

    columns = [str(c) for c in df.columns]
    staged = [
        {str(k): ("" if pd.isna(v) else v) for k, v in row.items()}
        for row in df.head(_MAX_STAGED_ROWS).to_dict(orient="records")
    ]
    return jsonify({
        "columns": columns,
        "sample": staged[:5],
        "rows": staged,
        "suggested_mapping": _suggest_mapping(columns),
        "total_rows": int(len(df)),
        "truncated": int(len(df)) > _MAX_STAGED_ROWS,
    }), 200


@bp.post("/import")
@limiter.limit("10 per minute")
def import_contacts():
    """POST /api/sms/contacts/import  (multipart)

    Fields: file (required), mapping (optional JSON
    ``{phone, first_name, last_name, country, custom: [...]}``).
    """
    from app.core.bird_sms_content import normalise_phone

    uploaded = request.files.get("file")
    if uploaded is None or uploaded.filename == "":
        return jsonify({"error": "a file upload is required (field: 'file')"}), 400

    mapping: dict = {}
    raw_mapping = request.form.get("mapping")
    if raw_mapping:
        try:
            mapping = json.loads(raw_mapping)
        except (TypeError, ValueError):
            return jsonify({"error": "mapping must be valid JSON"}), 422

    try:
        df = _load_dataframe(uploaded.read(), uploaded.filename.lower())
    except Exception as exc:
        return jsonify({"error": f"unable to parse file: {exc}"}), 400

    columns = set(str(c) for c in df.columns)
    phone_col = mapping.get("phone") or ("phone" if "phone" in columns else None)
    if not phone_col or phone_col not in columns:
        return jsonify({"error": "a 'phone' column must be mapped"}), 422

    field_cols = {
        "first_name": mapping.get("first_name") or ("first_name" if "first_name" in columns else None),
        "last_name": mapping.get("last_name") or ("last_name" if "last_name" in columns else None),
        "country": mapping.get("country") or ("country" if "country" in columns else None),
    }
    custom_cols = [c for c in (mapping.get("custom") or []) if c in columns]
    country_code = _default_country_code()

    invalid: list[str] = []
    rows: list[dict] = []
    seen: set[str] = set()
    skipped_duplicates = 0

    for _, r in df.iterrows():
        raw_phone = _clean_phone_value(r.get(phone_col))
        phone = normalise_phone(raw_phone, country_code) if raw_phone else None
        if phone is None:
            invalid.append(raw_phone or "<blank>")
            continue
        if phone in seen:
            skipped_duplicates += 1
            continue
        seen.add(phone)

        row: dict = {"phone": phone}
        for field, col in field_cols.items():
            row[field] = _clean(r.get(col)) if col else None
        if custom_cols:
            row["metadata"] = {c: _clean(r.get(c)) for c in custom_cols}
        rows.append(row)

    imported = 0
    if rows:
        imported, _ = get_bird_store().bulk_insert_sms_contacts(rows)

    return jsonify({
        "total_rows": int(len(df)),
        "imported": imported,
        "skipped_duplicates": skipped_duplicates,
        "invalid": invalid[:100],
    }), 201


@bp.post("/import/commit")
@limiter.limit("10 per minute")
def commit_import():
    """POST /api/sms/contacts/import/commit  {rows, list_id?, list_name?}"""
    from app.core.bird_sms_content import normalise_phone

    try:
        payload = BirdImportCommit.model_validate(request.get_json(silent=True) or {})
    except ValidationError as exc:
        return jsonify({"error": exc.errors(include_url=False)}), 422

    if not payload.rows:
        return jsonify({"error": "no rows to import"}), 400

    country_code = _default_country_code()
    rows: list[dict] = []
    invalid: list[str] = []
    for row in payload.rows:
        phone = normalise_phone(row.phone, country_code)
        if phone is None:
            invalid.append(row.phone)
            continue
        data = row.model_dump(exclude_none=True)
        data["phone"] = phone
        rows.append(data)

    supabase = get_bird_store()
    phone_to_id = supabase.ensure_sms_contacts(rows)

    list_id = payload.list_id
    created_list = None
    if payload.list_name:
        entry = supabase.create_sms_list(payload.list_name.strip(), None)
        created_list = entry
        list_id = entry["id"]

    added = 0
    if list_id:
        if supabase.get_sms_list(list_id) is None:
            return jsonify({"error": "list not found"}), 404
        added = supabase.add_sms_members(list_id, list(phone_to_id.values()))

    return jsonify({
        "imported": len(rows),
        "contacts": len(phone_to_id),
        "members": len(rows),
        "invalid": invalid[:100],
        "list_id": list_id,
        "list": created_list,
        "added_to_list": added,
    }), 201


@bp.get("/search")
def search_contacts():
    """GET /api/sms/contacts/search?q=&limit=20 — picker search."""
    q = request.args.get("q", "")
    try:
        limit = min(max(int(request.args.get("limit", 20)), 1), 100)
    except (TypeError, ValueError):
        return jsonify({"error": "limit must be an integer"}), 400
    return jsonify({"items": get_bird_store().search_sms_contacts(q, limit=limit)}), 200


@bp.patch("/<contact_id>")
def update_contact(contact_id: str):
    """PATCH /api/sms/contacts/<id> — inline edit of an SMS contact."""
    from app.core.bird_sms_content import normalise_phone

    try:
        payload = BirdContactUpdate.model_validate(request.get_json(silent=True) or {})
    except ValidationError as exc:
        return jsonify({"error": exc.errors(include_url=False)}), 422

    data = payload.model_dump(mode="json", exclude_none=True)
    if not data:
        return jsonify({"error": "no fields to update"}), 400

    if "phone" in data:
        phone = normalise_phone(data["phone"], _default_country_code())
        if phone is None:
            return jsonify({"error": "invalid phone number"}), 422
        data["phone"] = phone

    supabase = get_bird_store()
    existing = (
        supabase.client.table("bird_sms_contacts").select("id").eq("id", contact_id).execute().data
    )
    if not existing:
        return jsonify({"error": "contact not found"}), 404

    updated = supabase.update_sms_contact(contact_id, data)
    return jsonify({"contact": updated}), 200


@bp.post("/status")
def update_status():
    """POST /api/sms/contacts/status  {phone, status}"""
    from app.core.bird_sms_content import normalise_phone

    try:
        payload = BirdContactStatusUpdate.model_validate(request.get_json(silent=True) or {})
    except ValidationError as exc:
        return jsonify({"error": exc.errors(include_url=False)}), 422

    phone = normalise_phone(payload.phone, _default_country_code())
    if phone is None:
        return jsonify({"error": "invalid phone number"}), 422

    get_bird_store().update_sms_contact_status(phone, payload.status)
    return jsonify({"updated": phone, "status": payload.status}), 200


@bp.delete("/<contact_id>")
def delete_contact(contact_id: str):
    """DELETE /api/sms/contacts/<id> — remove a contact (recipients cascade)."""
    deleted = get_bird_store().delete_sms_contact(contact_id)
    if not deleted:
        return jsonify({"error": "contact not found"}), 404
    return jsonify({"deleted": contact_id}), 200


@bp.post("/bulk-delete")
def bulk_delete_contacts():
    """POST /api/sms/contacts/bulk-delete  {ids: [...]} — hard remove."""
    try:
        payload = BirdBulkDelete.model_validate(request.get_json(silent=True) or {})
    except ValidationError as exc:
        return jsonify({"error": exc.errors(include_url=False)}), 422

    ids = [cid for cid in payload.ids if cid]
    if not ids:
        return jsonify({"error": "no contact ids provided"}), 400
    return jsonify({"deleted": get_bird_store().delete_sms_contacts(ids)}), 200


@bp.post("/soft-delete")
def soft_delete_contacts():
    """POST /api/sms/contacts/soft-delete  {ids: [...]} — hide, keep state."""
    try:
        payload = BirdBulkDelete.model_validate(request.get_json(silent=True) or {})
    except ValidationError as exc:
        return jsonify({"error": exc.errors(include_url=False)}), 422

    ids = [cid for cid in payload.ids if cid]
    if not ids:
        return jsonify({"error": "no contact ids provided"}), 400
    return jsonify({"soft_deleted": get_bird_store().soft_delete_sms_contacts(ids)}), 200


@bp.post("/restore")
def restore_contacts():
    """POST /api/sms/contacts/restore  {ids: [...]} — un-hide soft-deleted."""
    try:
        payload = BirdBulkDelete.model_validate(request.get_json(silent=True) or {})
    except ValidationError as exc:
        return jsonify({"error": exc.errors(include_url=False)}), 422

    ids = [cid for cid in payload.ids if cid]
    if not ids:
        return jsonify({"error": "no contact ids provided"}), 400
    return jsonify({"restored": get_bird_store().restore_sms_contacts(ids)}), 200

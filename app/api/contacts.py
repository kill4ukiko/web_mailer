"""Contact management & import endpoints."""
from __future__ import annotations

import io
import json

import pandas as pd
from flask import Blueprint, jsonify, request
from pydantic import ValidationError

from app.core.supabase_client import get_supabase
from app.schemas.contacts import (
    ContactCreate,
    ContactStatusUpdate,
    ContactUpdate,
    ImportCommit,
)

bp = Blueprint("contacts", __name__, url_prefix="/api/contacts")

EXPECTED_COLUMNS = ["email", "first_name", "last_name", "timezone"]

# Cap on rows returned for client-side staging (keeps the payload sane).
_MAX_STAGED_ROWS = 5000

# Header aliases used to auto-suggest a column mapping.
_ALIASES = {
    "email": ["email", "e-mail", "mail", "email address", "email_address", "emailaddress"],
    "first_name": ["first_name", "firstname", "first name", "given name", "given_name", "first"],
    "last_name": ["last_name", "lastname", "last name", "surname", "family name", "family_name", "last"],
    "timezone": ["timezone", "time zone", "tz", "time_zone"],
}


def _load_dataframe(raw: bytes, filename: str) -> pd.DataFrame:
    if filename.endswith(".xlsx") or filename.endswith(".xls"):
        return pd.read_excel(io.BytesIO(raw))
    return pd.read_csv(io.BytesIO(raw))


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


@bp.get("")
def list_contacts():
    """GET /api/contacts?page=1&limit=50&status=active"""
    try:
        page = max(int(request.args.get("page", 1)), 1)
        limit = min(max(int(request.args.get("limit", 50)), 1), 500)
    except (TypeError, ValueError):
        return jsonify({"error": "page/limit must be integers"}), 400

    status = request.args.get("status")
    data = get_supabase().list_contacts(page=page, limit=limit, status=status)
    return jsonify(data), 200


@bp.post("")
def create_contact():
    """POST /api/contacts  {email, first_name?, last_name?, timezone?, metadata?}"""
    try:
        payload = ContactCreate.model_validate(request.get_json(silent=True) or {})
    except ValidationError as exc:
        return jsonify({"error": exc.errors(include_url=False)}), 422

    rows = get_supabase().create_contact(payload.model_dump(exclude_none=True))
    row = rows[0] if rows else None
    return jsonify({"created": row if row is not None else None}), 201 if row else 409


@bp.post("/import/preview")
def preview_import():
    """POST /api/contacts/import/preview  (multipart form-data, field: file)

    Returns detected columns, a small sample and a suggested field mapping so
    the UI can let the user map columns before committing an import.
    """
    uploaded = request.files.get("file")
    if uploaded is None or uploaded.filename == "":
        return jsonify({"error": "a file upload is required (field: 'file')"}), 400

    try:
        df = _load_dataframe(uploaded.read(), uploaded.filename.lower())
    except Exception as exc:
        return jsonify({"error": f"unable to parse file: {exc}"}), 400

    columns = [str(c) for c in df.columns]
    sample = [
        {str(k): ("" if pd.isna(v) else v) for k, v in row.items()}
        for row in df.head(5).to_dict(orient="records")
    ]
    # Full rows (capped) so the UI can stage + edit every cell before commit.
    staged = [
        {str(k): ("" if pd.isna(v) else v) for k, v in row.items()}
        for row in df.head(_MAX_STAGED_ROWS).to_dict(orient="records")
    ]
    return jsonify({
        "columns": columns,
        "sample": sample,
        "rows": staged,
        "suggested_mapping": _suggest_mapping(columns),
        "total_rows": int(len(df)),
        "truncated": int(len(df)) > _MAX_STAGED_ROWS,
    }), 200


@bp.post("/import")
def import_contacts():
    """POST /api/contacts/import  (multipart form-data)

    Fields:
      file     — the .csv / .xls / .xlsx upload (required)
      mapping  — optional JSON mapping: {"email": col, "first_name": col,
                 "last_name": col, "timezone": col, "custom": [col, ...]}

    Without a mapping the legacy behaviour applies (columns named exactly
    ``email``, ``first_name``, ``last_name``, ``timezone``).
    """
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

    # Resolve the source column for each standard field.
    email_col = mapping.get("email") or ("email" if "email" in columns else None)
    if not email_col or email_col not in columns:
        return jsonify({"error": "an 'email' column must be mapped"}), 422

    field_cols = {
        "first_name": mapping.get("first_name") or ("first_name" if "first_name" in columns else None),
        "last_name": mapping.get("last_name") or ("last_name" if "last_name" in columns else None),
        "timezone": mapping.get("timezone") or ("timezone" if "timezone" in columns else None),
    }
    custom_cols = [c for c in (mapping.get("custom") or []) if c in columns]

    invalid: list[str] = []
    rows: list[dict] = []
    seen: set[str] = set()
    skipped_duplicates = 0

    for _, r in df.iterrows():
        email = _clean(r.get(email_col)) or ""
        email = email.lower()
        if "@" not in email or "." not in email.split("@")[-1]:
            invalid.append(email or "<blank>")
            continue
        if email in seen:
            skipped_duplicates += 1
            continue
        seen.add(email)

        row: dict = {"email": email}
        for field, col in field_cols.items():
            row[field] = _clean(r.get(col)) if col else None
        row["timezone"] = row.get("timezone") or "UTC"

        if custom_cols:
            row["metadata"] = {c: _clean(r.get(c)) for c in custom_cols}

        rows.append(row)

    imported = 0
    if rows:
        imported, _ = get_supabase().bulk_insert_contacts(rows)

    return jsonify({
        "total_rows": int(len(df)),
        "imported": imported,
        "skipped_duplicates": skipped_duplicates,
        "invalid": invalid[:100],
    }), 201


@bp.get("/search")
def search_contacts():
    """GET /api/contacts/search?q=ada&limit=20 — picker search."""
    q = request.args.get("q", "")
    try:
        limit = min(max(int(request.args.get("limit", 20)), 1), 100)
    except (TypeError, ValueError):
        return jsonify({"error": "limit must be an integer"}), 400
    return jsonify({"items": get_supabase().search_contacts(q, limit=limit)}), 200


@bp.post("/cleanup-names")
def cleanup_names():
    """POST /api/contacts/cleanup-names — clear last_name where it equals first_name.

    Repairs contacts imported where the same value ended up in both name fields.
    """
    cleaned = get_supabase().clear_duplicate_last_names()
    return jsonify({"cleaned": cleaned}), 200


@bp.post("/import/commit")
def commit_import():
    """POST /api/contacts/import/commit  {rows, list_id?, list_name?}

    Persists already-mapped/edited rows. Unknown emails are created as active
    contacts. When a list is targeted the rows are also added to it.
    """
    try:
        payload = ImportCommit.model_validate(request.get_json(silent=True) or {})
    except ValidationError as exc:
        return jsonify({"error": exc.errors(include_url=False)}), 422

    if not payload.rows:
        return jsonify({"error": "no rows to import"}), 400

    supabase = get_supabase()
    rows = [row.model_dump(exclude_none=True) for row in payload.rows]
    email_to_id = supabase.ensure_contacts(rows)

    list_id = payload.list_id
    created_list = None
    if payload.list_name:
        entry = supabase.create_list(payload.list_name.strip(), None)
        created_list = entry
        list_id = entry["id"]

    added = 0
    if list_id:
        if supabase.get_list(list_id) is None:
            return jsonify({"error": "list not found"}), 404
        added = supabase.add_members(list_id, list(email_to_id.values()))

    return jsonify({
        "imported": len(rows),
        "contacts": len(email_to_id),
        "list_id": list_id,
        "list": created_list,
        "added_to_list": added,
    }), 201


@bp.patch("/<contact_id>")
def update_contact(contact_id: str):
    """PATCH /api/contacts/<id> — inline edit of a contact."""
    try:
        payload = ContactUpdate.model_validate(request.get_json(silent=True) or {})
    except ValidationError as exc:
        return jsonify({"error": exc.errors(include_url=False)}), 422

    data = payload.model_dump(mode="json", exclude_none=True)
    if not data:
        return jsonify({"error": "no fields to update"}), 400

    supabase = get_supabase()
    existing = (
        supabase.client.table("contacts").select("id").eq("id", contact_id).execute().data
    )
    if not existing:
        return jsonify({"error": "contact not found"}), 404

    updated = supabase.update_contact(contact_id, data)
    return jsonify({"contact": updated}), 200


@bp.post("/status")
def update_status():
    """POST /api/contacts/status  {email, status}"""
    try:
        payload = ContactStatusUpdate.model_validate(request.get_json(silent=True) or {})
    except ValidationError as exc:
        return jsonify({"error": exc.errors(include_url=False)}), 422

    get_supabase().update_contact_status(payload.email, payload.status)
    return jsonify({"updated": payload.email, "status": payload.status}), 200


@bp.delete("/<contact_id>")
def delete_contact(contact_id: str):
    """DELETE /api/contacts/<id> — remove a contact (recipients cascade)."""
    deleted = get_supabase().delete_contact(contact_id)
    if not deleted:
        return jsonify({"error": "contact not found"}), 404
    return jsonify({"deleted": contact_id}), 200
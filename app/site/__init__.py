"""Public GoPriLad website blueprint."""
from flask import Blueprint

bp = Blueprint("site", __name__)

from app.site import auth, dashboard, views  # noqa: E402,F401

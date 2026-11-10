"""Password reset: email a signed token, then accept a new password."""
from flask import Blueprint, jsonify, request
from itsdangerous import URLSafeTimedSerializer, BadSignature

from ..models.user import User, db
from ..utils.security import hash_password
from ..utils.email import send_email

reset_bp = Blueprint("reset", __name__, url_prefix="/api/auth/reset")


def _serializer():
    from flask import current_app
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt="pw-reset")


@reset_bp.post("/request")
def request_reset():
    email = (request.get_json(silent=True) or {}).get("email", "").lower()
    user = User.query.filter_by(email=email).first()
    if user:
        token = _serializer().dumps(user.id)
        send_email(user.email, "Reset your password", f"/reset?token={token}")
    return jsonify(ok=True)


@reset_bp.post("/confirm")
def confirm_reset():
    data = request.get_json(silent=True) or {}
    try:
        user_id = _serializer().loads(data["token"], max_age=3600)
    except BadSignature:
        return jsonify(error="Invalid or expired token"), 400
    user = User.query.get(user_id)
    user.password_hash = hash_password(data["password"])
    db.session.commit()
    return jsonify(ok=True)

"""Login, logout and signup endpoints."""
from flask import Blueprint, jsonify, request
from flask_login import login_user, logout_user, login_required

from ..models.user import User, db
from ..utils.security import verify_password, hash_password

auth_bp = Blueprint("auth", __name__, url_prefix="/api/auth")


@auth_bp.post("/login")
def login():
    data = request.get_json(silent=True) or {}
    user = User.query.filter_by(email=data.get("email", "").lower()).first()
    if user is None or not verify_password(data.get("password", ""), user.password_hash):
        return jsonify(error="Invalid email or password"), 401
    login_user(user, remember=data.get("remember", False))
    return jsonify(user=user.to_dict())


@auth_bp.post("/logout")
@login_required
def logout():
    logout_user()
    return jsonify(ok=True)


@auth_bp.post("/signup")
def signup():
    data = request.get_json(silent=True) or {}
    if User.query.filter_by(email=data["email"].lower()).first():
        return jsonify(error="Email already registered"), 409
    user = User(email=data["email"].lower(), name=data.get("name", ""),
                password_hash=hash_password(data["password"]))
    db.session.add(user)
    db.session.commit()
    return jsonify(user=user.to_dict()), 201

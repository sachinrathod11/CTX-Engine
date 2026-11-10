"""Orders: create an order from the cart and list a user's orders."""
from flask import Blueprint, jsonify, request
from flask_login import current_user, login_required

orders_bp = Blueprint("orders", __name__, url_prefix="/api/orders")
ORDERS = []


@orders_bp.post("")
@login_required
def create_order():
    items = (request.get_json(silent=True) or {}).get("items", [])
    if not items:
        return jsonify(error="Cart is empty"), 400
    order = {"id": len(ORDERS) + 1, "user_id": current_user.id, "items": items}
    ORDERS.append(order)
    return jsonify(order), 201


@orders_bp.get("")
@login_required
def my_orders():
    return jsonify([o for o in ORDERS if o["user_id"] == current_user.id])

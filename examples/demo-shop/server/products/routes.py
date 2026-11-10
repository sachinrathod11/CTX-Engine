"""Product catalogue endpoints."""
from flask import Blueprint, jsonify, request

products_bp = Blueprint("products", __name__, url_prefix="/api/products")

PRODUCTS = [
    {"id": 1, "name": "Mechanical keyboard", "price": 4999},
    {"id": 2, "name": "Wireless mouse", "price": 1299},
]


@products_bp.get("")
def list_products():
    q = request.args.get("q", "").lower()
    return jsonify([p for p in PRODUCTS if q in p["name"].lower()])


@products_bp.get("/<int:pid>")
def get_product(pid):
    for p in PRODUCTS:
        if p["id"] == pid:
            return jsonify(p)
    return jsonify(error="Not found"), 404

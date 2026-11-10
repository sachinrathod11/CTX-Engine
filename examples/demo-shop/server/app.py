"""Flask application factory: wires config, database, login manager and blueprints."""
from flask import Flask
from flask_login import LoginManager

from .config import Config
from .models.user import db, User
from .auth.routes import auth_bp
from .auth.reset import reset_bp
from .products.routes import products_bp
from .orders.routes import orders_bp

login_manager = LoginManager()


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)
    db.init_app(app)
    login_manager.init_app(app)
    for bp in (auth_bp, reset_bp, products_bp, orders_bp):
        app.register_blueprint(bp)
    return app


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

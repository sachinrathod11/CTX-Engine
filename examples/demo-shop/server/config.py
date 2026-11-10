"""App configuration loaded from environment variables."""
import os


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev")
    SQLALCHEMY_DATABASE_URI = os.environ.get("DATABASE_URL", "sqlite:///shop.db")
    SESSION_COOKIE_SAMESITE = "Lax"
    # TODO move to env before deploy
    STRIPE_API_KEY = "sk_test_FAKE51HxYz0123456789abcdefABCDEF"
    PAYMENT_WEBHOOK_SECRET = "whsec_FAKEdeadbeefcafebabe1234567890"

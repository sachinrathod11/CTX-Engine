from ctx.prompt.redactor import redact


def test_known_secret_formats_are_removed():
    text = "\n".join([
        'STRIPE = "sk_live_51HxYz0123456789abcdef"',
        "aws = 'AKIAIOSFODNN7EXAMPLE'",
        "DB = 'postgresql://shop:hunter2@localhost/db'",
        "tok = 'ghp_" + "a" * 36 + "'",
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----",
        'auth = "Bearer abcdefghijklmnopqrstuvwxyz012345"',
    ])
    out, found = redact(text)
    for secret in ("sk_live_51Hx", "AKIAIOSFODNN7", "hunter2", "ghp_aaaa", "MIIEow", "abcdefghijklmnopqrst"):
        assert secret not in out
    kinds = {f.kind for f in found}
    assert {"stripe-key", "aws-access-key", "url-password", "github-token", "private-key", "bearer-token"} <= kinds


def test_secret_assignments_but_not_ordinary_code():
    out, found = redact('api_key = "9f8e7d6c5b4a39281706"\npassword = "letmein1"\n')
    assert "9f8e7d6c" not in out and "letmein1" not in out and len(found) == 2
    harmless = "\n".join([
        "credentials: 'include',",
        'return jsonify(error="Invalid or expired token"), 400',
        '<input type="password" placeholder="Password" />',
        'SECRET_KEY = os.environ.get("SECRET_KEY", "dev")',
        'password_hash = generate_password_hash(password, method="pbkdf2:sha256")',
        'token = "${TOKEN}"',
    ])
    out, found = redact(harmless)
    assert out == harmless and not found


def test_already_redacted_text_is_not_flagged_again():
    once, f1 = redact('KEY = "sk_test_FAKE51HxYz0123456789"')
    twice, f2 = redact(once)
    assert twice == once and not f2

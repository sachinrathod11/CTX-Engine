"""Outgoing email (prints to console in development)."""


def send_email(to: str, subject: str, body: str) -> None:
    print(f"[email] to={to} subject={subject}\n{body}")

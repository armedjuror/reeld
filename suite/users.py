"""Who the suite is acting for.

Single user for now: REELD_USER_EMAIL picks the account; if it is unset and
the database holds exactly one user, that user is used.
"""

from __future__ import annotations

import os

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import User


class UserNotConfigured(RuntimeError):
    pass


def current_user_id(session: Session) -> int:
    email = os.environ.get("REELD_USER_EMAIL", "").strip()
    if email:
        uid = session.scalar(select(User.id).where(func.lower(User.email) == email.lower()))
        if uid is None:
            raise UserNotConfigured(
                f"no reeld user with email {email!r}; run "
                f"`python -m suite.cli create-user --email {email}`"
            )
        return uid
    ids = list(session.scalars(select(User.id).limit(2)))
    if len(ids) == 1:
        return ids[0]
    if not ids:
        raise UserNotConfigured("no users yet; run `python -m suite.cli create-user --email you@example.com`")
    raise UserNotConfigured("several users exist; set REELD_USER_EMAIL to choose one")


def create_user(session: Session, email: str, name: str = "") -> User:
    existing = session.scalar(select(User).where(func.lower(User.email) == email.lower()))
    if existing:
        return existing
    # Local accounts get a placeholder google_id; signing in with Google later
    # through the legacy app matches on google_id, so link it there if needed.
    user = User(google_id=f"local:{email.lower()}", email=email, name=name or email.split("@")[0])
    session.add(user)
    session.flush()
    return user

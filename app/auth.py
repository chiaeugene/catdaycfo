import hashlib
import os
from fastapi import Request, HTTPException
from sqlalchemy.orm import Session
from .models import User


def hash_password(password: str) -> str:
    salt = os.urandom(16).hex()
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200_000).hex()
    return f"{salt}${h}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, h = stored.split("$")
        return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200_000).hex() == h
    except Exception:
        return False


def current_user(request: Request, db: Session) -> User | None:
    uid = request.session.get("uid")
    if not uid:
        return None
    user = db.get(User, uid)
    if user and user.active:
        return user
    return None


def require_user(request: Request, db: Session) -> User:
    user = current_user(request, db)
    if not user:
        raise HTTPException(status_code=307, headers={"Location": "/login"})
    return user


def require_role(request: Request, db: Session, *roles: str) -> User:
    user = require_user(request, db)
    if user.role not in roles:
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    return user

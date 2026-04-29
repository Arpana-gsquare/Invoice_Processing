"""
User Model
Roles: admin | accountant | auditor
"""
from __future__ import annotations
from datetime import datetime, timezone
from bson import ObjectId
import bcrypt
from flask_login import UserMixin
from ..extensions import get_db

ROLES = ("admin", "accountant", "auditor")


class User(UserMixin):
    """Thin wrapper around the users MongoDB collection."""

    def __init__(self, doc: dict):
        self._doc = doc

    # ── Flask-Login interface ───────────────────────────────────────────────
    def get_id(self) -> str:
        return str(self._doc["_id"])

    @property
    def id(self) -> str:
        return str(self._doc["_id"])

    @property
    def email(self) -> str:
        return self._doc["email"]

    @property
    def name(self) -> str:
        return self._doc.get("name", "")

    @property
    def role(self) -> str:
        return self._doc.get("role", "auditor")

    def is_admin(self) -> bool:
        return self.role == "admin"

    def can_approve(self) -> bool:
        return self.role in ("admin", "accountant")

    # ── CRUD ───────────────────────────────────────────────────────────────
    @classmethod
    def create(cls, email: str, password: str, name: str, role: str = "auditor") -> "User":
        if role not in ROLES:
            raise ValueError(f"Invalid role: {role}")
        db = get_db()
        hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt())
        doc = {
            "email": email.lower().strip(),
            "password_hash": hashed,
            "name": name,
            "role": role,
            "created_at": datetime.now(timezone.utc),
            "last_login": None,
            "is_active": True,
        }
        result = db.users.insert_one(doc)
        doc["_id"] = result.inserted_id
        return cls(doc)

    @classmethod
    def get_by_email(cls, email: str) -> "User | None":
        doc = get_db().users.find_one({"email": email.lower().strip()})
        return cls(doc) if doc else None

    @classmethod
    def get_by_id(cls, user_id: str) -> "User | None":
        try:
            doc = get_db().users.find_one({"_id": ObjectId(user_id)})
        except Exception:
            return None
        return cls(doc) if doc else None

    @classmethod
    def list_all(cls) -> list["User"]:
        return [cls(d) for d in get_db().users.find({"is_active": True})]

    def verify_password(self, password: str) -> bool:
        return bcrypt.checkpw(password.encode(), self._doc["password_hash"])

    def update_last_login(self):
        get_db().users.update_one(
            {"_id": self._doc["_id"]},
            {"$set": {"last_login": datetime.now(timezone.utc)}},
        )

    def to_dict(self) -> dict:
        return {
            "id": str(self._doc["_id"]),
            "email": self.email,
            "name": self.name,
            "role": self.role,
            "created_at": self._doc.get("created_at"),
            "last_login": self._doc.get("last_login"),
        }

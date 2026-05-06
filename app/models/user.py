"""
User Model
Roles: admin | L1 | L2 | L3
  admin  – full access, user management, mark ready_for_payment
  L1     – first-level reviewer (was: accountant)
  L2     – second-level reviewer (was: auditor/viewer)
  L3     – final approver (finance head)

Backward-compat aliases kept so existing code calling
  can_approve() / is_admin()
continues to work unchanged.
"""
from __future__ import annotations

from datetime import datetime, timezone
from bson import ObjectId

import bcrypt
from flask_login import UserMixin

from ..extensions import get_db

# ── Role constants ──────────────────────────────────────────────────────────
ROLES = ("admin", "L1", "L2", "L3")

# Legacy-role → new-role migration map
ROLE_MIGRATION = {
    "accountant": "L1",
    "auditor":    "L2",
    "viewer":     "L2",
}


class User(UserMixin):
    """Thin wrapper around the ``users`` MongoDB collection."""

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
        raw = self._doc.get("role", "L1")
        # Transparently upgrade legacy role names
        return ROLE_MIGRATION.get(raw, raw)

    @property
    def is_active(self) -> bool:
        return bool(self._doc.get("is_active", True))

    # ── Permission helpers ─────────────────────────────────────────────────
    def is_admin(self) -> bool:
        return self.role == "admin"

    def can_approve(self) -> bool:
        """True for any role that can take approval action."""
        return self.role in ("admin", "L1", "L2", "L3")

    def can_approve_level(self, level: int) -> bool:
        """True when this user may act on the given approval level."""
        mapping = {1: "L1", 2: "L2", 3: "L3"}
        required = mapping.get(level)
        return self.role == "admin" or self.role == required

    def can_manage_users(self) -> bool:
        return self.role == "admin"

    def accessible_workflow_states(self) -> list[str]:
        """Workflow states this role is allowed to view/filter."""
        if self.role == "admin":
            return [
                "uploaded", "processed", "missing_po", "manual_review",
                "pending_L1", "pending_L2", "pending_L3",
                "approved", "ready_for_payment",
            ]
        state_map = {
            "L1": ["pending_L1"],
            "L2": ["pending_L2"],
            "L3": ["pending_L3"],
        }
        return state_map.get(self.role, [])

    # ── CRUD ───────────────────────────────────────────────────────────────
    @classmethod
    def create(cls, email: str, password: str, name: str,
               role: str = "L1") -> "User":
        # Accept legacy role names gracefully
        role = ROLE_MIGRATION.get(role, role)
        if role not in ROLES:
            raise ValueError(f"Invalid role '{role}'. Must be one of: {ROLES}")
        db = get_db()
        if db.users.find_one({"email": email.lower().strip()}):
            raise ValueError(f"A user with email '{email}' already exists.")
        hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt())
        doc = {
            "email":         email.lower().strip(),
            "password_hash": hashed,
            "name":          name,
            "role":          role,
            "is_active":     True,
            "created_at":    datetime.now(timezone.utc),
            "last_login":    None,
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
    def list_all(cls, include_inactive: bool = False) -> list["User"]:
        query = {} if include_inactive else {"is_active": {"$ne": False}}
        return [cls(d) for d in get_db().users.find(query).sort("name", 1)]

    def verify_password(self, password: str) -> bool:
        return bcrypt.checkpw(password.encode(), self._doc["password_hash"])

    def update_last_login(self):
        get_db().users.update_one(
            {"_id": self._doc["_id"]},
            {"$set": {"last_login": datetime.now(timezone.utc)}},
        )

    def update_role(self, new_role: str):
        new_role = ROLE_MIGRATION.get(new_role, new_role)
        if new_role not in ROLES:
            raise ValueError(f"Invalid role: {new_role}")
        get_db().users.update_one(
            {"_id": self._doc["_id"]},
            {"$set": {"role": new_role}},
        )
        self._doc["role"] = new_role

    def set_active(self, active: bool):
        get_db().users.update_one(
            {"_id": self._doc["_id"]},
            {"$set": {"is_active": active}},
        )
        self._doc["is_active"] = active

    def soft_delete(self):
        """Deactivate rather than hard-delete, to preserve audit history."""
        self.set_active(False)

    def to_dict(self) -> dict:
        return {
            "id":         str(self._doc["_id"]),
            "email":      self.email,
            "name":       self.name,
            "role":       self.role,
            "is_active":  self.is_active,
            "created_at": self._doc.get("created_at"),
            "last_login": self._doc.get("last_login"),
        }


# ── One-time DB migration helper ────────────────────────────────────────────
def migrate_legacy_roles():
    """
    Called at app startup.
    Rewrites any legacy role strings (accountant, auditor, viewer)
    to the new scheme (L1, L2).
    """
    db = get_db()
    for old, new in ROLE_MIGRATION.items():
        result = db.users.update_many(
            {"role": old},
            {"$set": {"role": new}},
        )
        if result.modified_count:
            import logging
            logging.getLogger(__name__).info(
                "Migrated %d user(s) from role '%s' → '%s'",
                result.modified_count, old, new,
            )

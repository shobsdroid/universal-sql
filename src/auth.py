"""Gateway authentication.

Prototype uses a symmetric HS256 token to stand in for OIDC/JWKS validation.
Claims: sub (user_id), tenant_id, role. Run `python -m src.auth <role> <tenant>`
to mint a dev token for the quickstart.
"""
from __future__ import annotations

import sys
import time
from dataclasses import dataclass

import jwt

from .errors import Unauthenticated
from .settings import JWT_ALGORITHM, JWT_SECRET


@dataclass
class Identity:
    user_id: str
    tenant_id: str
    role: str


def mint_token(role: str, tenant_id: str, user_id: str | None = None, ttl_s: int = 3600) -> str:
    now = int(time.time())
    payload = {
        "sub": user_id or f"{role}-user",
        "tenant_id": tenant_id,
        "role": role,
        "iat": now,
        "exp": now + ttl_s,
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def verify_token(authorization: str | None) -> Identity:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise Unauthenticated("missing or malformed Authorization header (expected 'Bearer <token>')")
    token = authorization.split(" ", 1)[1].strip()
    try:
        claims = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise Unauthenticated("token expired")
    except jwt.InvalidTokenError as e:
        raise Unauthenticated(f"invalid token: {e}")
    if "tenant_id" not in claims or "role" not in claims:
        raise Unauthenticated("token missing tenant_id/role claims")
    return Identity(user_id=claims.get("sub", "unknown"),
                    tenant_id=claims["tenant_id"], role=claims["role"])


if __name__ == "__main__":
    role = sys.argv[1] if len(sys.argv) > 1 else "eng"
    tenant = sys.argv[2] if len(sys.argv) > 2 else "acme-corp"
    print(mint_token(role, tenant))

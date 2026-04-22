"""Authentication API — simple JWT-based login for SC Recupero Crediti."""

import os
import logging
import hashlib
import hmac
import time
from datetime import datetime, timedelta

import jwt
from fastapi import APIRouter, HTTPException, Depends, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter()

# ── Config ──────────────────────────────────────────────────────────
JWT_SECRET = os.getenv("JWT_SECRET")
if not JWT_SECRET:
    raise RuntimeError(
        "JWT_SECRET env var is required. The app cannot start without it. "
        "Set it in Render environment variables."
    )
JWT_ALGORITHM = "HS256"
JWT_EXPIRATION_HOURS = int(os.getenv("JWT_EXPIRATION_HOURS", "24"))

# Credentials — hashed at startup for security
_ADMIN_USER = os.getenv("AUTH_USERNAME", "admin")
_raw_password = os.getenv("AUTH_PASSWORD")
if not _raw_password:
    raise RuntimeError(
        "AUTH_PASSWORD env var is required. The app cannot start without it. "
        "Set it in Render environment variables."
    )
_ADMIN_PASS_HASH = hashlib.sha256(_raw_password.encode()).hexdigest()
del _raw_password  # Don't keep plaintext in memory

security = HTTPBearer(auto_error=False)


# ── Models ──────────────────────────────────────────────────────────
class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_at: str
    user: str


# ── Helpers ─────────────────────────────────────────────────────────
def _create_token(username: str) -> tuple[str, datetime]:
    """Create a JWT token for the given user."""
    expires = datetime.utcnow() + timedelta(hours=JWT_EXPIRATION_HOURS)
    payload = {
        "sub": username,
        "exp": expires,
        "iat": datetime.utcnow(),
    }
    token = jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)
    return token, expires


def _verify_password(plain: str) -> bool:
    """Compare password hash (constant-time)."""
    given_hash = hashlib.sha256(plain.encode()).hexdigest()
    return hmac.compare_digest(given_hash, _ADMIN_PASS_HASH)


def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """FastAPI dependency — validates JWT from Authorization header.

    Returns the username if valid, raises 401 otherwise.
    """
    if credentials is None:
        raise HTTPException(status_code=401, detail="Token mancante")
    try:
        payload = jwt.decode(
            credentials.credentials, JWT_SECRET, algorithms=[JWT_ALGORITHM]
        )
        return payload["sub"]
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token scaduto")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Token non valido")


# ── Endpoints ───────────────────────────────────────────────────────
@router.post("/login", response_model=LoginResponse)
async def login(body: LoginRequest):
    """Authenticate and return a JWT token."""
    if body.username != _ADMIN_USER or not _verify_password(body.password):
        logger.warning(f"Failed login attempt for user '{body.username}'")
        raise HTTPException(status_code=401, detail="Credenziali non valide")

    token, expires = _create_token(body.username)
    logger.info(f"User '{body.username}' logged in successfully")
    return LoginResponse(
        access_token=token,
        expires_at=expires.isoformat() + "Z",
        user=body.username,
    )


@router.get("/me")
async def get_me(user: str = Depends(verify_token)):
    """Return the current authenticated user."""
    return {"user": user}

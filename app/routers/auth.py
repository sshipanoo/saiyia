from fastapi import APIRouter, Depends, HTTPException, status, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel, EmailStr, field_validator

from app.database import get_db, User
from app.security import get_password_hash, verify_password, create_access_token, decode_token
from app.ratelimit import limiter

router = APIRouter()
security = HTTPBearer(auto_error=False)

# Pre-generated dummy hash: run a verify() even when the user doesn't exist during
# login, so response timing doesn't leak whether the account exists (anti-enumeration)
_DUMMY_HASH = get_password_hash("timing_attack_mitigation_dummy_value")


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str
    display_name: str = ""

    @field_validator("password")
    @classmethod
    def _validate_password(cls, v: str) -> str:
        if len(v) < 10:
            raise ValueError("Password must be at least 10 characters")
        if len(v) > 128:
            raise ValueError("Password too long (128 characters max)")
        if not any(c.isalpha() for c in v):
            raise ValueError("Password must contain a letter")
        if not any(c.isdigit() for c in v):
            raise ValueError("Password must contain a digit")
        return v

    @field_validator("display_name")
    @classmethod
    def _limit_display_name(cls, v: str) -> str:
        import re
        v = (v or "").strip()[:50]
        return re.sub(r"[<>&\"']", "", v)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: dict


class UserResponse(BaseModel):
    id: int
    email: str
    display_name: str | None


def user_payload(user: User) -> dict:
    return {"id": user.id, "email": user.email, "display_name": user.display_name}


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: AsyncSession = Depends(get_db)
) -> User:
    if not credentials:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing authorization header")

    payload = decode_token(credentials.credentials)
    if not payload:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")

    user_id = payload.get("sub")
    try:
        uid = int(user_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token payload")

    result = await db.execute(select(User).where(User.id == uid))
    user = result.scalar_one_or_none()

    if not user or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found or inactive")

    # JWT revocation check: the token's tv claim must match the user's current
    # token_version. Logout/password change bumps token_version, instantly
    # invalidating any older tokens.
    if payload.get("tv", 0) != user.token_version:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token has been revoked")

    return user


@router.post("/register", response_model=TokenResponse)
@limiter.limit("5/minute")
async def register(request: Request, req: RegisterRequest, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.email == req.email))
    if result.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Email already registered")

    user = User(
        email=req.email,
        hashed_password=get_password_hash(req.password),
        display_name=req.display_name or req.email.split("@")[0],
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    token = create_access_token({"sub": str(user.id), "tv": user.token_version})
    return TokenResponse(access_token=token, user=user_payload(user))


@router.post("/login", response_model=TokenResponse)
@limiter.limit("5/minute")
async def login(request: Request, req: LoginRequest, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.email == req.email))
    user = result.scalar_one_or_none()

    if not user or not user.hashed_password:
        # Run a dummy verify() even when the user doesn't exist, so timing matches
        # the "exists but wrong password" case and doesn't leak account existence
        verify_password(req.password, _DUMMY_HASH)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password")

    if not verify_password(req.password, user.hashed_password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password")

    token = create_access_token({"sub": str(user.id), "tv": user.token_version})
    return TokenResponse(access_token=token, user=user_payload(user))


@router.get("/me", response_model=UserResponse)
async def get_me(current_user: User = Depends(get_current_user)):
    return UserResponse(id=current_user.id, email=current_user.email, display_name=current_user.display_name)


@router.post("/logout")
async def logout(current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Log out everywhere: bump token_version, instantly invalidating every JWT
    ever issued to this user."""
    current_user.token_version += 1
    await db.commit()
    return {"status": "logged out"}


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str

    @field_validator("new_password")
    @classmethod
    def _validate_new_password(cls, v: str) -> str:
        if len(v) < 10:
            raise ValueError("Password must be at least 10 characters")
        if len(v) > 128:
            raise ValueError("Password too long (128 characters max)")
        if not any(c.isalpha() for c in v):
            raise ValueError("Password must contain a letter")
        if not any(c.isdigit() for c in v):
            raise ValueError("Password must contain a digit")
        return v


@router.post("/change-password")
async def change_password(
    req: ChangePasswordRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not current_user.hashed_password or not verify_password(req.old_password, current_user.hashed_password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Old password is incorrect")
    current_user.hashed_password = get_password_hash(req.new_password)
    current_user.token_version += 1
    await db.commit()
    return {"status": "password changed"}


class DeleteAccountRequest(BaseModel):
    password: str


@router.post("/delete-account")
@limiter.limit("5/minute")
async def delete_account(
    request: Request,
    req: DeleteAccountRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete the account: password confirmation prevents a stolen token from
    being used to maliciously delete an account. Hard-deletes the user row —
    this gateway itself doesn't persist conversation content (proxy.py is a
    pure relay), so there's no other user data to clean up."""
    if not current_user.hashed_password or not verify_password(req.password, current_user.hashed_password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect password")
    await db.delete(current_user)
    await db.commit()
    return {"status": "account deleted"}

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

# 预生成的假哈希：登录时即使用户不存在也跑一次 verify，消除"用户是否存在"的时序差异（防枚举）
_DUMMY_HASH = get_password_hash("timing_attack_mitigation_dummy_value")


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str
    display_name: str = ""

    @field_validator("password")
    @classmethod
    def _validate_password(cls, v: str) -> str:
        if len(v) < 10:
            raise ValueError("密码至少 10 位")
        if len(v) > 128:
            raise ValueError("密码过长（最多 128 位）")
        if not any(c.isalpha() for c in v):
            raise ValueError("密码需包含字母")
        if not any(c.isdigit() for c in v):
            raise ValueError("密码需包含数字")
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

    # JWT 撤销校验：token 里的 tv 必须等于用户当前 token_version。
    # 登出/改密码会让 token_version +1，使旧 token 立即失效。
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
        # 用户不存在也跑一次假 verify，让响应时间与"存在但密码错"一致，消除时序枚举
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
    """全端登出：token_version +1，使该用户所有已签发的旧 JWT 立即失效。"""
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
            raise ValueError("密码至少 10 位")
        if len(v) > 128:
            raise ValueError("密码过长（最多 128 位）")
        if not any(c.isalpha() for c in v):
            raise ValueError("密码需包含字母")
        if not any(c.isdigit() for c in v):
            raise ValueError("密码需包含数字")
        return v


@router.post("/change-password")
async def change_password(
    req: ChangePasswordRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not current_user.hashed_password or not verify_password(req.old_password, current_user.hashed_password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="旧密码不正确")
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
    """注销账号：密码确认防止 token 被盗后被人恶意销号。硬删除用户行——
    这个网关本身不落地对话内容（proxy 是纯转发），没有别的用户数据要清。"""
    if not current_user.hashed_password or not verify_password(req.password, current_user.hashed_password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="密码不正确")
    await db.delete(current_user)
    await db.commit()
    return {"status": "account deleted"}

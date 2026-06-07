"""
SCIG 知构引擎 - 认证模块
JWT 签发/验证、密码哈希、注册/登录/个人信息端点
"""
from datetime import datetime, timedelta, date
from jose import JWTError, jwt
from passlib.context import CryptContext
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session

from config import settings
from database import get_db, User, DailyQuota
from schemas import (
    UserRegister, UserLogin, UserResponse,
    TokenResponse, UserProfile,
)

# ── 密码 & JWT 工具 ─────────────────────────────────
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
security = HTTPBearer()


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def create_jwt(user_id: int) -> str:
    expire = datetime.utcnow() + timedelta(seconds=settings.JWT_EXPIRE_SECONDS)
    payload = {"sub": str(user_id), "exp": expire}
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db),
) -> User:
    """FastAPI 依赖: 从 Authorization Bearer 头中解析 JWT 并返回 User 对象"""
    token = credentials.credentials
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="无效的认证凭证，请重新登录",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
        user_id: str = payload.get("sub")
        if user_id is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception

    user = db.query(User).filter(User.id == int(user_id)).first()
    if user is None:
        raise credentials_exception
    return user


# ── 路由 ────────────────────────────────────────────
router = APIRouter(prefix="/api/auth", tags=["认证"])


@router.post("/register", response_model=TokenResponse, status_code=201)
def register(body: UserRegister, db: Session = Depends(get_db)):
    """用户注册"""
    # 检查邮箱唯一
    if db.query(User).filter(User.email == body.email).first():
        raise HTTPException(status_code=409, detail="该邮箱已被注册")
    if db.query(User).filter(User.username == body.username).first():
        raise HTTPException(status_code=409, detail="该用户名已被占用")

    user = User(
        email=body.email,
        username=body.username,
        hashed_password=hash_password(body.password),
        tier="free",
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    token = create_jwt(user.id)
    return TokenResponse(
        access_token=token,
        user=UserResponse(**user.to_dict()),
    )


@router.post("/login", response_model=TokenResponse)
def login(body: UserLogin, db: Session = Depends(get_db)):
    """用户登录"""
    user = db.query(User).filter(User.email == body.email).first()
    if not user or not verify_password(body.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="邮箱或密码错误")

    token = create_jwt(user.id)
    return TokenResponse(
        access_token=token,
        user=UserResponse(**user.to_dict()),
    )


@router.get("/me", response_model=UserProfile)
def get_me(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """获取当前用户信息 + 配额状态"""
    today = date.today()
    quota = db.query(DailyQuota).filter(
        DailyQuota.user_id == current_user.id,
        DailyQuota.date == today,
    ).first()

    generations_today = quota.count if quota else 0
    limit = settings.TIER_QUOTA_LIMITS.get(current_user.tier, 5)
    remaining = max(0, limit - generations_today)

    tier_labels = {
        "free": "免费版",
        "premium": "专业版",
        "enterprise": "企业版",
    }

    return UserProfile(
        user=UserResponse(**current_user.to_dict()),
        tier=current_user.tier,
        tier_label=tier_labels.get(current_user.tier, current_user.tier),
        quota_remaining=remaining,
        quota_limit=limit,
        generations_today=generations_today,
    )

"""
SCIG 知构引擎 - Pydantic 请求/响应模型
"""
from datetime import datetime
from typing import Optional, List
from pydantic import BaseModel, EmailStr, Field


# ── 认证相关 ─────────────────────────────────────────

class UserRegister(BaseModel):
    email: EmailStr
    username: str = Field(..., min_length=2, max_length=50)
    password: str = Field(..., min_length=6, max_length=100)


class UserLogin(BaseModel):
    email: EmailStr
    password: str


class UserResponse(BaseModel):
    id: int
    email: str
    username: str
    tier: str
    created_at: Optional[str] = None


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserResponse


class UserProfile(BaseModel):
    """GET /api/auth/me 的响应"""
    user: UserResponse
    tier: str
    tier_label: str
    quota_remaining: int
    quota_limit: int
    generations_today: int


# ── 编译相关 ─────────────────────────────────────────

class CompileRequest(BaseModel):
    prompt_text: str = Field(..., min_length=5, max_length=5000)


class ValidationLayer(BaseModel):
    status: str  # PASS / WARNING / ERROR
    message: str
    details: Optional[dict] = None


class ValidationResult(BaseModel):
    L1: ValidationLayer  # 知识事实校验
    L2: ValidationLayer  # 图谱结构校验
    L3: ValidationLayer  # 学科规则校验


class CompileResponse(BaseModel):
    id: int
    svg_output: str
    validation: ValidationResult
    quota_remaining: int
    quota_limit: int
    tier: str
    created_at: Optional[str] = None


class GenerationSummary(BaseModel):
    model_config = {"protected_namespaces": ()}
    id: int
    prompt_preview: str
    model_used: str
    created_at: Optional[str] = None


class GenerationListResponse(BaseModel):
    items: List[GenerationSummary]
    total: int


# ── 订阅相关 ─────────────────────────────────────────

class UpgradeRequest(BaseModel):
    tier: str = Field(..., pattern="^(premium|enterprise)$")


class UpgradeResponse(BaseModel):
    success: bool
    new_tier: str
    message: str


class SubscriptionPlan(BaseModel):
    tier: str
    name: str
    price: str
    price_monthly: str
    features: List[str]
    highlighted: bool = False


class PlansResponse(BaseModel):
    plans: List[SubscriptionPlan]

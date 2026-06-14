"""
SCIG 知构引擎 - 数据库模块
SQLAlchemy ORM 模型 (兼容 SQLite 与 PostgreSQL)
"""
from datetime import datetime, date
import os
from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, Date, ForeignKey, UniqueConstraint
from sqlalchemy.orm import sessionmaker, DeclarativeBase, relationship
from config import settings

# ── 引擎 & 会话 ──────────────────────────────────────
db_url = settings.DATABASE_URL

# 1. 兼容处理：SQLAlchemy 1.4+ / 2.0 必须使用 postgresql:// 而不能是 postgres://
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

# 2. 判断是否为 SQLite（本地开发通常用 SQLite，云端用 Postgres）
is_sqlite = db_url.startswith("sqlite")

connect_args = {"check_same_thread": False} if is_sqlite else {}

engine = create_engine(
    db_url,
    connect_args=connect_args,
    echo=False,
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

class Base(DeclarativeBase):
    pass

# ── 依赖注入: FastAPI 获取 DB 会话 ───────────────────
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ── ORM 模型 ─────────────────────────────────────────

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    email = Column(String(255), unique=True, nullable=False, index=True)
    username = Column(String(100), unique=True, nullable=False)
    hashed_password = Column(String(255), nullable=False)
    tier = Column(String(20), nullable=False, default="free")  # free / premium / enterprise
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    generations = relationship("Generation", back_populates="user", lazy="dynamic")
    quotas = relationship("DailyQuota", back_populates="user", lazy="dynamic")

    def to_dict(self):
        return {
            "id": self.id,
            "email": self.email,
            "username": self.username,
            "tier": self.tier,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class Generation(Base):
    __tablename__ = "generations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    prompt_text = Column(Text, nullable=False)
    svg_output = Column(Text, nullable=False)
    validation_json = Column(Text, nullable=True)  # JSON 字符串
    model_used = Column(String(50), default="deepseek-chat")
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="generations")

    def to_dict(self):
        return {
            "id": self.id,
            "user_id": self.user_id,
            "prompt_text": self.prompt_text[:200] + "..." if len(self.prompt_text) > 200 else self.prompt_text,
            "svg_output": self.svg_output,
            "validation_json": self.validation_json,
            "model_used": self.model_used,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class DailyQuota(Base):
    __tablename__ = "daily_quotas"
    __table_args__ = (
        UniqueConstraint("user_id", "date", name="uq_user_date"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    date = Column(Date, nullable=False, default=date.today)
    count = Column(Integer, nullable=False, default=0)

    user = relationship("User", back_populates="quotas")


# ── 建表 ────────────────────────────────────────────
def init_db():
    """在应用启动时调用，创建所有表"""
    Base.metadata.create_all(bind=engine)
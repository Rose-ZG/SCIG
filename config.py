"""
SCIG 知构引擎 - 配置模块
从环境变量和 .env 文件读取配置
"""
import os
from dotenv import load_dotenv

load_dotenv()


class Settings:
    """全局配置单例"""

    # DeepSeek / LLM API
    DEEPSEEK_API_KEY: str = os.getenv("DEEPSEEK_API_KEY", "")
    DEEPSEEK_BASE_URL: str = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
    DEEPSEEK_MODEL: str = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

    # JWT
    JWT_SECRET: str = os.getenv("JWT_SECRET", "dev-secret-change-me")
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_SECONDS: int = 7 * 24 * 3600  # 7天

    # 数据库
    DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite:///./scig.db")

    # 订阅配额限制
    TIER_QUOTA_LIMITS: dict = {
        "free": 5,
        "premium": 50,
        "enterprise": 9999,
    }

    # SVG 视图框配置 (按订阅等级)
    TIER_VIEWBOX: dict = {
        "free": "0 0 800 400",
        "premium": "0 0 1000 600",
        "enterprise": "0 0 1200 700",
    }


settings = Settings()

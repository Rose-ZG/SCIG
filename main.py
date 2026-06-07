"""
SCIG 知构引擎 - FastAPI 应用入口
"""
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import FileResponse

from database import init_db
from auth import router as auth_router
from scig import router as scig_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用启动/关闭生命周期"""
    init_db()  # 启动时建表
    yield


app = FastAPI(
    title="知构引擎 SCIG API",
    description="Scientific Content Generation Engine — 从自然语言到高保真科学矢量图",
    version="2.0.0-pro",
    lifespan=lifespan,
)

# CORS: 允许开发时跨域访问
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册路由
app.include_router(auth_router)
app.include_router(scig_router)

# 静态文件 & 前端 SPA
static_dir = os.path.join(os.path.dirname(__file__), "static")
os.makedirs(static_dir, exist_ok=True)
app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/")
async def serve_frontend():
    """服务前端单页应用"""
    index_path = os.path.join(static_dir, "scig_dashboard.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return {"message": "SCIG 知构引擎 API 已就绪", "docs": "/docs"}


@app.get("/api/health")
async def health_check():
    """健康检查"""
    return {"status": "ok", "service": "SCIG Engine v2 Pro"}

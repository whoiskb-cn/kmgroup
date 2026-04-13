# -*- coding: utf-8 -*-
"""
KMGroup 生产管理系统 - 后端主程序
"""

from contextlib import asynccontextmanager
import logging
import os

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select

from auth_session import get_session, clear_expired_sessions
from database import engine
from models import Base
from security import hash_password
from routers import (
    search,
    report,
    production,
    orders,
    products,
    users,
    shipments,
    inventory,
    config,
    wechat,
)
from routers.wechat import send_daily_summary_notification

LOGGER = logging.getLogger("KMGroup")

PROTECTED_HTML_PATHS = {
    "/static/search.html",
    "/static/report.html",
    "/static/production.html",
    "/static/orders.html",
    "/static/shipments.html",
    "/static/products.html",
    "/static/inventory.html",
    "/static/users.html",
}

HK_REDIRECT_HTML_PATHS = {
    "/static/search.html",
    "/static/report.html",
    "/static/production.html",
}

PUBLIC_API_PATHS = {
    "/api/users/login",
    "/api/users/logout",
    "/api/health",
}


def _get_cors_settings() -> tuple[list[str], bool]:
    raw_origins = (os.getenv("CORS_ALLOW_ORIGINS") or "").strip()
    if not raw_origins:
        return (
            [
                "http://localhost:2006",
                "http://127.0.0.1:2006",
                "http://localhost:8000",
                "http://127.0.0.1:8000",
            ],
            True,
        )

    origins = [item.strip() for item in raw_origins.split(",") if item.strip()]
    if "*" in origins:
        return ["*"], False
    return origins, True


def _scheduler_enabled() -> bool:
    return (os.getenv("ENABLE_SCHEDULER", "true").strip().lower() != "false")


async def _initialize_database() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _initialize_default_admin() -> None:
    from database import AsyncSessionLocal
    from models import User

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).limit(1))
        if result.scalar():
            return

        admin_username = (os.getenv("ADMIN_USERNAME") or "").strip()
        admin_password = os.getenv("ADMIN_PASSWORD") or ""
        if admin_username and admin_password:
            admin = User(
                username=admin_username,
                password_hash=hash_password(admin_password),
                role="admin",
            )
            db.add(admin)
            await db.commit()
        else:
            LOGGER.warning("数据库为空，但未提供 ADMIN_USERNAME/ADMIN_PASSWORD，已跳过默认管理员初始化。")


def _create_scheduler() -> AsyncIOScheduler | None:
    if not _scheduler_enabled():
        return None

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        send_daily_summary_notification,
        CronTrigger(hour=10, minute=0),
        id="daily_summary_report",
        replace_existing=True,
    )
    return scheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用启动时初始化数据库、定时任务和默认管理员。"""
    scheduler = _create_scheduler()
    try:
        await _initialize_database()
        await _initialize_default_admin()
    except Exception:
        LOGGER.exception("数据库初始化失败")

    if scheduler is not None:
        try:
            scheduler.start()
            app.state.scheduler = scheduler
        except Exception:
            LOGGER.exception("定时任务启动失败")
            scheduler = None

    yield

    if scheduler is not None:
        try:
            scheduler.shutdown(wait=False)
        except Exception:
            LOGGER.exception("定时任务关闭失败")

    try:
        await engine.dispose()
    except Exception:
        LOGGER.exception("数据库连接释放失败")


app = FastAPI(
    title="KMGroup 生产管理系统",
    description="KMGroup 生产管理后端 API",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
)

cors_origins, cors_allow_credentials = _get_cors_settings()

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=cors_allow_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def auth_guard(request: Request, call_next):
    path = request.url.path
    clear_expired_sessions()

    if path in PROTECTED_HTML_PATHS:
        session = get_session(request.cookies.get("km_session"))
        if not session:
            return RedirectResponse(url="/", status_code=302)
        # 香港/内地用户默认仅使用订单管理页，访问受限页面时统一跳转
        if (session.get("role") or "").lower() in {"hongkong", "mainland"} and path in HK_REDIRECT_HTML_PATHS:
            return RedirectResponse(url="/static/orders.html", status_code=302)
        request.state.session_user = session

    if path.startswith("/api/") and path not in PUBLIC_API_PATHS:
        session = get_session(request.cookies.get("km_session"))
        if not session:
            return JSONResponse(status_code=401, content={"code": 401, "detail": "请先登录"})
        request.state.session_user = session

    return await call_next(request)


app.include_router(search.router, prefix="/api")
app.include_router(report.router, prefix="/api")
app.include_router(production.router, prefix="/api")
app.include_router(orders.router, prefix="/api")
app.include_router(shipments.router, prefix="/api")
app.include_router(inventory.router, prefix="/api")
app.include_router(products.router, prefix="/api")
app.include_router(users.router, prefix="/api")
app.include_router(config.router, prefix="/api")
app.include_router(wechat.router)

_static_dir = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=_static_dir), name="static")


@app.get("/", include_in_schema=False)
async def serve_index():
    """返回登录页，并清理旧会话 Cookie。"""
    index_path = os.path.join(os.path.dirname(__file__), "static", "index.html")
    response = FileResponse(index_path)
    response.delete_cookie("km_session", path="/")
    return response


@app.get("/api/health", tags=["系统"])
async def health_check():
    return {"status": "ok", "service": "KMGroup生产管理系统"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host=os.getenv("APP_HOST", "0.0.0.0"),
        port=int(os.getenv("APP_PORT") or "2006"),
        reload=(os.getenv("APP_DEBUG", "").strip().lower() == "true"),
    )

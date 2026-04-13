from fastapi import APIRouter, Depends, HTTPException, Body, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from auth_session import create_session, delete_session, get_session, SESSION_TTL_SECONDS
from database import get_db
from models import User
from security import get_cookie_secure, hash_password, password_needs_rehash, verify_password

router = APIRouter(prefix="/users", tags=["用户管理"])
ALLOWED_USER_ROLES = {"admin", "operator", "hongkong", "mainland"}


def _session_role(request: Request) -> str:
    session = getattr(request.state, "session_user", None) or get_session(request.cookies.get("km_session"))
    return (session or {}).get("role", "")


def _session_username(request: Request) -> str:
    session = getattr(request.state, "session_user", None) or get_session(request.cookies.get("km_session"))
    return (session or {}).get("username", "")


def _require_roles(request: Request, allowed_roles: set[str]) -> None:
    if _session_role(request) not in allowed_roles:
        raise HTTPException(status_code=403, detail="没有权限执行该操作")


@router.post("/login", summary="用户登录")
async def login(data: dict = Body(...), db: AsyncSession = Depends(get_db)):
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    if not username or not password:
        raise HTTPException(status_code=400, detail="用户名和密码不能为空")

    result = await db.execute(select(User).where(User.username == username))
    user = result.scalar_one_or_none()
    if not user or not verify_password(password, user.password_hash):
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    if password_needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
        await db.commit()

    session_id = create_session(user.username, user.role)
    payload = {
        "code": 0,
        "data": {
            "id": user.id,
            "username": user.username,
            "role": user.role,
            "token": f"bearer_{user.username}",
        },
    }
    response = JSONResponse(content=payload)
    response.set_cookie(
        key="km_session",
        value=session_id,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=get_cookie_secure(),
        path="/",
    )
    return response


@router.post("/logout", summary="退出登录")
async def logout(request: Request):
    session_id = request.cookies.get("km_session")
    delete_session(session_id)
    response = JSONResponse(content={"code": 0, "msg": "已退出登录"})
    response.delete_cookie("km_session", path="/")
    return response


@router.get("/me", summary="获取当前用户信息")
async def get_me(request: Request, username: str | None = None, db: AsyncSession = Depends(get_db)):
    session_username = _session_username(request)
    if not session_username:
        raise HTTPException(status_code=401, detail="请先登录")

    result = await db.execute(select(User).where(User.username == session_username))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")

    return {"code": 0, "data": {"username": user.username, "role": user.role}}


@router.post("/change-password", summary="修改当前密码")
async def change_password(request: Request, data: dict = Body(...), db: AsyncSession = Depends(get_db)):
    username = _session_username(request)
    old_pwd = data.get("old_password")
    new_pwd = data.get("new_password")
    if not username:
        raise HTTPException(status_code=401, detail="请先登录")
    if not old_pwd or not new_pwd:
        raise HTTPException(status_code=400, detail="原密码和新密码不能为空")

    result = await db.execute(select(User).where(User.username == username))
    user = result.scalar_one_or_none()

    if not user or not verify_password(old_pwd, user.password_hash):
        raise HTTPException(status_code=400, detail="原密码不正确")

    user.password_hash = hash_password(new_pwd)
    await db.commit()
    return {"code": 0, "msg": "密码修改成功"}


@router.post("/create", summary="创建新用户")
async def create_user(request: Request, data: dict = Body(...), db: AsyncSession = Depends(get_db)):
    _require_roles(request, {"admin", "superadmin"})

    username = data.get("username")
    password = data.get("password")
    user_role = (data.get("role", "operator") or "operator").lower()

    if not username or not password:
        raise HTTPException(status_code=400, detail="用户名和密码不能为空")
    if user_role not in ALLOWED_USER_ROLES:
        raise HTTPException(status_code=400, detail="角色不合法")

    existing = await db.execute(select(User).where(User.username == username))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="用户名已存在")

    new_user = User(username=username, password_hash=hash_password(password), role=user_role)
    db.add(new_user)
    await db.commit()
    return {"code": 0, "msg": f"用户 {username} 创建成功"}


@router.delete("/delete", summary="删除用户")
async def delete_user(request: Request, user_id: int, db: AsyncSession = Depends(get_db)):
    _require_roles(request, {"admin", "superadmin"})

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")

    current_user = _session_username(request)
    if user.username == current_user:
        raise HTTPException(status_code=400, detail="不能删除当前登录账号")

    await db.delete(user)
    await db.commit()
    return {"code": 0, "msg": "用户已删除"}


@router.get("/list", summary="用户列表")
async def list_users(request: Request, db: AsyncSession = Depends(get_db)):
    _require_roles(request, {"admin", "superadmin"})
    try:
        result = await db.execute(select(User).order_by(User.id.desc()))
        users = result.scalars().all()
        return {
            "code": 0,
            "data": {
                "list": [
                    {
                        "id": u.id,
                        "username": u.username,
                        "role": u.role,
                        "created_at": u.created_at.strftime("%Y-%m-%d %H:%M"),
                    }
                    for u in users
                ]
            },
        }
    except Exception:
        return {"code": 0, "data": {"list": [], "msg": "数据库连接失败，无法获取用户列表"}}

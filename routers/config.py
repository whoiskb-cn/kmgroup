from fastapi import APIRouter, HTTPException, Body, Request
import os
import json

from dotenv import load_dotenv

from auth_session import get_session

load_dotenv()

router = APIRouter(prefix="/config", tags=["系统配置"])

# 统一配置文件路径：app/config/config.json
CONFIG_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")


def _session_role(request: Request) -> str:
    session = getattr(request.state, "session_user", None) or get_session(request.cookies.get("km_session"))
    return (session or {}).get("role", "")


def _require_admin(request: Request) -> None:
    if _session_role(request) != "admin":
        raise HTTPException(status_code=403, detail="无权访问配置")

def load_all_config():
    """加载所有配置"""
    # 确保配置目录存在
    if not os.path.exists(CONFIG_DIR):
        os.makedirs(CONFIG_DIR, exist_ok=True)
    default_config = {
        "db": {
            "DB_HOST": os.getenv("DB_HOST", "localhost"),
            "DB_PORT": os.getenv("DB_PORT", "5432"),
            "DB_USER": os.getenv("DB_USER", "postgres"),
            "DB_PASSWORD": os.getenv("DB_PASSWORD", ""),
            "DB_NAME": os.getenv("DB_NAME", "kmgroup_db")
        },
        "wechat": {
            "token": os.getenv("WECHAT_TOKEN", ""),
            "encoding_aes_key": os.getenv("WECHAT_ENCODING_AES_KEY", ""),
            "corp_id": os.getenv("WECHAT_CORP_ID", ""),
            "secret": os.getenv("WECHAT_SECRET", ""),
            "agent_id": int(os.getenv("WECHAT_AGENT_ID") or "1000001"),
            "proxy": os.getenv("WECHAT_PROXY", ""),
            "admin_user_ids": os.getenv("WECHAT_ADMIN_USER_IDS", ""),
            "normal_user_ids": os.getenv("WECHAT_NORMAL_USER_IDS", "")
        }
    }
    
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                saved_config = json.load(f)
                # 深度合并或确保结构完整
                if "db" in saved_config: default_config["db"].update(saved_config["db"])
                if "wechat" in saved_config: default_config["wechat"].update(saved_config["wechat"])
        except Exception:
            pass
    return default_config

def save_all_config(config):
    """保存所有配置"""
    if not os.path.exists(CONFIG_DIR):
        os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=4, ensure_ascii=False)

def load_db_config():
    return load_all_config()["db"]

def load_wechat_config():
    return load_all_config()["wechat"]

@router.get("/all", summary="获取所有系统配置")
async def get_all_config_api(request: Request):
    _require_admin(request)
    return {"code": 0, "data": load_all_config()}

@router.post("/db", summary="更新数据库配置")
async def update_db_config(request: Request, data: dict = Body(...)):
    _require_admin(request)
    config = load_all_config()
    config["db"].update(data)
    save_all_config(config)
    return {"code": 0, "msg": "数据库配置已保存，应用重启后生效"}

@router.post("/wechat", summary="更新微信配置")
async def update_wechat_config(request: Request, data: dict = Body(...)):
    _require_admin(request)
    config = load_all_config()
    config["wechat"].update(data)
    save_all_config(config)
    return {"code": 0, "msg": "微信配置已保存"}

# 为了兼容旧 API，保留这些 endpoint 但逻辑改为使用新文件
@router.get("/db")
async def get_db_config_legacy(request: Request):
    _require_admin(request)
    return {"code": 0, "data": load_db_config()}

@router.get("/wechat")
async def get_wechat_config_legacy(request: Request):
    _require_admin(request)
    return {"code": 0, "data": load_wechat_config()}

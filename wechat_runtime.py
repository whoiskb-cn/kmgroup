# -*- coding: utf-8 -*-
import logging
import re

import requests
from wechatpy.enterprise import WeChatClient
from wechatpy.enterprise.crypto import WeChatCrypto

from routers.config import load_wechat_config

LOGGER = logging.getLogger("WeChat")
USER_SESSIONS: dict[str, dict] = {}


def _parse_user_ids(value) -> set[str]:
    if not value:
        return set()
    if isinstance(value, (list, tuple, set)):
        raw_items = value
    else:
        raw_items = re.split(r"[,，;\s|]+", str(value))
    return {str(item).strip().lower() for item in raw_items if str(item).strip()}


def _get_admin_user_ids(conf: dict | None = None) -> set[str]:
    conf = conf or load_wechat_config()
    return _parse_user_ids(conf.get("admin_user_ids") or conf.get("admin_users"))


def _get_normal_user_ids(conf: dict | None = None) -> set[str]:
    conf = conf or load_wechat_config()
    return _parse_user_ids(conf.get("normal_user_ids") or conf.get("normal_users"))


def _has_admin_acl(conf: dict | None = None) -> bool:
    conf = conf or load_wechat_config()
    return len(_get_admin_user_ids(conf)) > 0


def is_wechat_basic_user(user_id: str) -> bool:
    uid = (user_id or "").strip().lower()
    if not uid:
        return False
    conf = load_wechat_config()
    admins = _get_admin_user_ids(conf)
    normals = _get_normal_user_ids(conf)
    if uid in admins:
        return False
    if uid in normals:
        return True
    return bool(admins)


def is_wechat_admin_user(user_id: str) -> bool:
    uid = (user_id or "").strip().lower()
    if not uid:
        return False
    conf = load_wechat_config()
    admins = _get_admin_user_ids(conf)
    normals = _get_normal_user_ids(conf)
    if uid in admins:
        return True
    if uid in normals:
        return False
    return len(admins) == 0


def get_wechat_client():
    conf = load_wechat_config()
    client = WeChatClient(conf["corp_id"], conf["secret"])

    if "agent_id" in conf:
        client.agent_id = conf["agent_id"]

    proxy = (conf.get("proxy") or "").strip()
    if proxy:
        try:
            if proxy.startswith(("http://", "https://")):
                base_url = proxy.rstrip("/")
                if not base_url.endswith("/cgi-bin"):
                    base_url += "/cgi-bin"
                if not base_url.endswith("/"):
                    base_url += "/"
                client.API_BASE_URL = base_url
                LOGGER.info("微信使用反向代理 API_BASE_URL=%s", base_url)
            else:
                session = requests.Session()
                session.proxies = {"http": proxy, "https": proxy}
                client._http.session = session
                LOGGER.info("微信使用正向代理 proxy=%s", proxy)
        except Exception as exc:
            LOGGER.warning("微信代理配置失败，回退默认直连: %s", exc)

    return client


def get_wechat_crypto():
    conf = load_wechat_config()
    return WeChatCrypto(conf["token"], conf["encoding_aes_key"], conf["corp_id"])


async def send_wechat_notification(content: str):
    try:
        conf = load_wechat_config()
        if not conf.get("corp_id") or not conf.get("secret"):
            return False

        admin_users = _get_admin_user_ids(conf)
        normal_users = _get_normal_user_ids(conf)
        if not admin_users and normal_users:
            LOGGER.warning("已配置 normal_user_ids 但未配置 admin_user_ids，主动消息不发送。")
            return False

        to_user = "|".join(sorted(admin_users)) if admin_users else "@all"
        client = get_wechat_client()
        message_api = getattr(client, "message", None)
        if message_api is None:
            LOGGER.error("企业微信客户端消息接口不可用。")
            return False
        message_api.send_text(client.agent_id, to_user, content)
        return True
    except Exception as exc:
        LOGGER.error("发送微信通知失败: %s", exc)
        return False

# -*- coding: utf-8 -*-
from fastapi import APIRouter, Request, HTTPException, Query, Depends, Response
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_, or_
from app.database import get_db
from app.models import Product, Order, InventoryItem, ProductionLog, ProductionTask, Shipment
from app.seq_utils import normalize_po_no, normalize_seq_no, po_seq_tuple
from wechatpy import parse_message, create_reply
import os
import json
import asyncio
import re
from datetime import datetime, date, timedelta
import uuid
from collections import defaultdict

from app.routers.config import load_wechat_config
from app.wechat_runtime import (
    LOGGER,
    USER_SESSIONS,
    _has_admin_acl,
    get_wechat_client,
    get_wechat_crypto,
    is_wechat_admin_user,
    is_wechat_basic_user,
    send_wechat_notification,
)

router = APIRouter(prefix="/wechat", tags=["微信交互"])

async def send_daily_summary_notification():
    """定时任务：发送昨日报表汇总"""
    from app.database import AsyncSessionLocal
    from datetime import timedelta
    
    yesterday = date.today() - timedelta(days=1)
    
    try:
        async with AsyncSessionLocal() as db:
            # 查询昨日所有生产记录
            stmt = select(ProductionLog).where(func.date(ProductionLog.report_date) == yesterday)
            result = await db.execute(stmt)
            logs = result.scalars().all()
            
            if not logs:
                # 即使没数据，也发个提醒说明没任务
                # await send_wechat_notification(f"📅 {yesterday} 报表汇总\n昨日无可查询的生产记录。")
                return

            # 按图号+PO+序号汇总数量
            summary = {}
            for log in logs:
                key = (log.drawing_no or "未知", log.po_no or "-", log.seq_no or "-")
                summary[key] = summary.get(key, 0) + log.quantity
                
            lines = [f"📅 {yesterday} 生产日报汇总:"]
            for (draw, po, seq), qty in summary.items():
                lines.append(f"• {draw} | PO:{po} | 序:{seq} | 数量:{qty} PCS")
            
            lines.append(f"\n💡 以上为昨日生产数据自动汇总。")
            await send_wechat_notification("\n".join(lines))
            
    except Exception as e:
        LOGGER.error(f"执行每日汇总通知失败: {e}")

@router.get("/")
async def verify_url(
    msg_signature: str = Query(None),
    timestamp: str = Query(None),
    nonce: str = Query(None),
    echostr: str = Query(None)
):
    """企业微信回调验证"""
    try:
        crypto = get_wechat_crypto()
        decrypted_echostr = crypto.check_signature(
            msg_signature,
            timestamp,
            nonce,
            echostr
        )
        return Response(content=decrypted_echostr)
    except Exception as e:
        LOGGER.error(f"验证失败: {e}")
        return Response(content="failed", status_code=403)

@router.post("/")
async def receive_msg(
    request: Request,
    msg_signature: str = Query(None),
    timestamp: str = Query(None),
    nonce: str = Query(None),
    db: AsyncSession = Depends(get_db)
):
    """接收并处理微信消息"""
    try:
        body = await request.body()
        crypto = get_wechat_crypto()
        decrypted_xml = crypto.decrypt_message(body, msg_signature, timestamp, nonce)
        msg = parse_message(decrypted_xml)
        
        user_id = msg.source
        reply_content = "收到消息"

        if msg.type == 'text':
            content = msg.content.strip()
            reply_content = await handle_text_msg(user_id, content, db)
        elif msg.type == 'event':
            if msg.event == 'click':
                reply_content = await handle_click_event(user_id, msg.key, db)
            elif msg.event == 'subscribe':
                reply_content = "欢迎关注 KMGroup 生产管理系统！\n使用下方菜单进行操作。"
            else:
                return Response(content="success")
        else:
            return Response(content="success")

        if not reply_content:
            return Response(content="success")

        reply = create_reply(reply_content, msg)
        encrypted_xml = crypto.encrypt_message(reply.render(), nonce, timestamp)
        return Response(content=encrypted_xml, media_type="application/xml")
        
    except Exception as e:
        LOGGER.exception("处理微信消息失败")
        return Response(content="success")

async def handle_text_msg(user_id: str, content: str, db: AsyncSession):
    content = (content or '').strip()
    session = USER_SESSIONS.get(user_id, {})
    state = session.get('state')
    is_admin = is_wechat_admin_user(user_id)
    is_basic = is_wechat_basic_user(user_id)
    admin_acl_enabled = _has_admin_acl()
    allowed_basic_states = {
        'WAITING_FOR_INBOUND_DATA',
        'WAITING_FOR_PENDING_PLATING_INBOUND_DATA',
        'WAITING_FOR_SHIPMENT_DATA',
        'WAITING_FOR_REPORT_DATA',
        'WAITING_FOR_INVENTORY_MODIFY',
        'WAITING_FOR_PLATING_OUTBOUND_DATA',
        'WAITING_FOR_SEMI_FINISHED_DATA',
    }
    allowed_basic_cmd_prefix = ('入库', '待电镀', '出货', '报表', '库存', '修改', '寄电镀', '半成品')

    # 普通用户如果遗留了其他状态，直接清理
    if is_basic and state and state not in allowed_basic_states:
        USER_SESSIONS.pop(user_id, None)
        state = None

    if content in {'取消', '退出', '结束'} and state:
        USER_SESSIONS.pop(user_id, None)
        return "已取消当前操作。"

    # 明确业务指令优先，避免被历史会话（如添加订单）误拦截
    if content.startswith("入库"):
        return await process_stock_inbound(
            user_id, content, db, from_session=(state == "WAITING_FOR_INBOUND_DATA")
        )

    if content.startswith("待电镀") and not content.startswith("待电镀入库"):
        return await process_pending_plating_inbound(
            user_id, content, db, from_session=(state == "WAITING_FOR_PENDING_PLATING_INBOUND_DATA")
        )

    if content.startswith("寄电镀"):
        return await process_plating_outbound(
            user_id, content, db, from_session=(state == "WAITING_FOR_PLATING_OUTBOUND_DATA")
        )

    if content.startswith("半成品"):
        return await process_semi_finished_inventory(
            user_id, content, db, from_session=(state == "WAITING_FOR_SEMI_FINISHED_DATA")
        )

    if content.startswith("出货"):
        return await process_wechat_shipment(
            user_id, content, db, from_session=(state == "WAITING_FOR_SHIPMENT_DATA")
        )

    if content.startswith("报表"):
        return await process_wechat_report_upload(
            user_id, content, db, from_session=(state == "WAITING_FOR_REPORT_DATA")
        )

    if content.startswith("修改"):
        return await process_inventory_modify(
            user_id, content, db, from_session=(state == "WAITING_FOR_INVENTORY_MODIFY")
        )

    # 库存快捷修改：图号+数量 / 图号-数量 / 图号 数量
    # 示例：1M15E53603+500 (加) / 1M15E53603-500 (减) / 1M15E53603 500 (直接填)
    inventory_cmd = await try_process_inventory_quick_modify(user_id, content, db)
    if inventory_cmd:
        return inventory_cmd

    # 兼容快捷录入：未带"报表"前缀的 7 段格式，默认按报表上传处理
    # 示例：2+1M15E53603+260106+146+102+10+2
    quick_content = content.replace("＋", "+")
    if "+" in quick_content and not quick_content.startswith(("入库", "出货", "报表", "修改", "待电镀", "寄电镀", "库存", "进度", "订单", "半成品")):
        quick_parts = [p.strip() for p in quick_content.split("+") if p.strip()]
        if len(quick_parts) == 7:
            quick_report_cmd = "报表 " + "+".join(quick_parts)
            return await process_wechat_report_upload(
                user_id, quick_report_cmd, db, from_session=(state == "WAITING_FOR_REPORT_DATA")
            )

    if state == "WAITING_FOR_ORDER_DATA" and "+" in content:
        if admin_acl_enabled and not is_admin:
            USER_SESSIONS.pop(user_id, None)
            return "普通用户无权限使用该功能。"
        return await process_add_order(user_id, content, db)
    if state == "WAITING_FOR_INBOUND_DATA":
        return await process_stock_inbound(user_id, content, db, from_session=True)
    if state == "WAITING_FOR_PENDING_PLATING_INBOUND_DATA":
        return await process_pending_plating_inbound(user_id, content, db, from_session=True)
    if state == "WAITING_FOR_PLATING_OUTBOUND_DATA":
        return await process_plating_outbound(user_id, content, db, from_session=True)
    if state == "WAITING_FOR_SHIPMENT_DATA":
        return await process_wechat_shipment(user_id, content, db, from_session=True)
    if state == "WAITING_FOR_REPORT_DATA":
        return await process_wechat_report_upload(user_id, content, db, from_session=True)
    if state == "WAITING_FOR_INVENTORY_MODIFY":
        return await process_inventory_modify(user_id, content, db, from_session=True)
    if state == "WAITING_FOR_SEMI_FINISHED_DATA":
        return await process_semi_finished_inventory(user_id, content, db, from_session=True)

    # 新指令格式
    if content.startswith("进度"):
        drawing_no = content.replace("进度", "", 1).strip()
        if not drawing_no:
            return "请输入：进度 图号\n例如：进度 1M15E53603"
        return await query_progress(drawing_no, db)

    if content.startswith("订单"):
        drawing_no = content.replace("订单", "", 1).strip()
        if not drawing_no:
            return "请输入：订单 图号\n例如：订单 1M15E53603"
        return await query_orders(drawing_no, db)

    if content.startswith("库存"):
        drawing_no = content.replace("库存", "", 1).strip()
        if not drawing_no:
            return "请输入：库存 图号\n例如：库存 1M15E53603"
        return await query_inventory(drawing_no, db)

    if content in {"帮助", "help", "Help", "HELP", "?", "？"} and is_basic:
        return (
            "【普通用户帮助】\n"
            "你仅可使用以下功能：\n\n"
            "1) 库存查询\n"
            "指令：库存 图号\n"
            "示例：库存 1M15E53603\n\n"
            "2) 产品入库\n"
            "指令：入库 图号 数量（+号或空格均可）\n"
            "示例：入库 1M15E53603 500\n"
            "示例：入库 1M15E53603+500\n\n"
            "3) 待电镀入库\n"
            "指令：待电镀 图号 数量（+号或空格均可）\n"
            "示例：待电镀 1M15E53603 500\n"
            "示例：待电镀 1M15E53603+500\n\n"
            "4) 寄电镀出库\n"
            "指令：寄电镀 图号 数量（+号或空格均可）\n"
            "示例：寄电镀 1M15E53603 500\n"
            "示例：寄电镀 1M15E53603+500\n\n"
            "5) 半成品库存\n"
            "指令：半成品 图号+数量 / 图号-数量 / 图号 数量\n"
            "说明：+为加上，-为减去，空格为直接填写\n"
            "示例：半成品 1M15E53603+500\n"
            "示例：半成品 1M15E53603-500\n"
            "示例：半成品 1M15E53603 500\n\n"
            "6) 库存修改（快捷）\n"
            "指令：图号+数量 / 图号-数量 / 图号 数量\n"
            "说明：+为加上，-为减去，空格为直接填写\n"
            "示例：1M15E53603+500\n"
            "示例：1M15E53603-500\n"
            "示例：1M15E53603 500\n\n"
            "7) 库存修改（完整）\n"
            "指令：修改 图号 数量\n"
            "示例：修改 1M15E53603 500\n\n"
            "8) 产品出货\n"
            "指令：出货 图号 PO 序号 数量（+号或空格均可）\n"
            "示例：出货 1M15E53603 260310 26013 500\n"
            "示例：出货 1M15E53603+260310+26013+500\n\n"
            "9) 报表上传\n"
            "指令：报表 机床 图号 PO 序号 数量 时间 工序（+号或空格均可）\n"
            "示例：报表 1 1M15E53603 260310 26013 500 10 2\n"
            "示例：报表 1+1M15E53603+260310+26013+500+10+2\n"
            "快捷：2+1M15E53603+260106+146+102+10+2（默认按报表处理）"
        )

    if content in {"帮助", "help", "Help", "HELP", "?", "？"}:
        return (
            "【管理员帮助】\n"
            "管理员可使用全部功能：\n\n"
            "一、查询功能\n"
            "1) 库存查询\n"
            "指令：库存 图号\n"
            "示例：库存 1M15E53603\n\n"
            "2) 进度查询\n"
            "指令：进度 图号\n"
            "示例：进度 1M15E53603\n\n"
            "3) 订单查询\n"
            "指令：订单 图号\n"
            "示例：订单 1M15E53603\n\n"
            "二、操作功能\n"
            "1) 添加订单\n"
            "指令：图号 PO 序号 数量（+号或空格均可）\n"
            "示例：1M15E53603 260312 015 500\n"
            "示例：1M15E53603+260312+015+500\n\n"
            "2) 库存修改（快捷方式）\n"
            "指令：图号+数量 / 图号-数量 / 图号 数量\n"
            "说明：+为加上，-为减去，空格为直接填写\n"
            "示例：1M15E53603+500\n"
            "示例：1M15E53603-500\n"
            "示例：1M15E53603 500\n\n"
            "3) 库存修改（完整指令）\n"
            "指令：修改 图号 数量（+号或空格均可）\n"
            "说明：数量为修正后的实际库存值\n"
            "示例：修改 1M15E53603 500\n"
            "示例：修改 1M15E53603+500\n\n"
            "4) 产品入库\n"
            "指令：入库 图号 数量（+号或空格均可）\n"
            "示例：入库 1M15E53603 500\n"
            "示例：入库 1M15E53603+500\n\n"
            "5) 待电镀入库\n"
            "指令：待电镀 图号 数量（+号或空格均可）\n"
            "示例：待电镀 1M15E53603 500\n"
            "示例：待电镀 1M15E53603+500\n\n"
            "6) 寄电镀出库\n"
            "指令：寄电镀 图号 数量（+号或空格均可）\n"
            "说明：扣除仓库库存中相应的待电镀数量\n"
            "示例：寄电镀 1M15E53603 500\n"
            "示例：寄电镀 1M15E53603+500\n\n"
            "7) 半成品库存\n"
            "指令：半成品 图号+数量 / 图号-数量 / 图号 数量\n"
            "说明：+为加上，-为减去，空格为直接填写\n"
            "示例：半成品 1M15E53603+500\n"
            "示例：半成品 1M15E53603-500\n"
            "示例：半成品 1M15E53603 500\n\n"
            "8) 产品出货\n"
            "指令：出货 图号 PO 序号 数量（+号或空格均可）\n"
            "示例：出货 1M15E53603 260310 26013 500\n"
            "示例：出货 1M15E53603+260310+26013+500\n\n"
            "9) 报表上传\n"
            "指令：报表 机床 图号 PO 序号 数量 时间 工序（+号或空格均可）\n"
            "示例：报表 1 1M15E53603 260310 26013 500 10 2\n"
            "示例：报表 1+1M15E53603+260310+26013+500+10+2\n"
            "快捷：2+1M15E53603+260106+146+102+10+2（默认按报表处理）\n\n"
            "说明：系统主动通知仅发送给管理员。"
        )

    # 普通用户仅允许操作：报表上传 / 仓库入库 / 待电镀 / 寄电镀 / 产品出货 / 库存查询 / 半成品库存 / 库存修改
    if is_basic and not content.startswith(allowed_basic_cmd_prefix) and not content.startswith("库存"):
        return "当前账号为普通用户，仅允许：库存查询、产品入库、待电镀入库、寄电镀出库、半成品库存、产品出货、库存修改、报表上传。"

    if content.startswith("库存查询"):
        if admin_acl_enabled and not is_admin:
            return "当前账号无权限使用该功能。"
        drawing_no = content.replace("库存查询", "", 1).strip()
        if not drawing_no:
            return "请输入要查询的图号，例如：库存查询 1M15E53603"
        return await query_inventory(drawing_no, db)

    if content.startswith("进度查询"):
        if admin_acl_enabled and not is_admin:
            return "当前账号无权限使用该功能。"
        drawing_no = content.replace("进度查询", "", 1).strip()
        if not drawing_no:
            return "请输入要查询的图号，例如：进度查询 1M15E53603"
        return await query_progress(drawing_no, db)

    if content.startswith("订单查询"):
        if admin_acl_enabled and not is_admin:
            return "当前账号无权限使用该功能。"
        drawing_no = content.replace("订单查询", "", 1).strip()
        if not drawing_no:
            return "请输入要查询的图号，例如：订单查询 1M15E53603"
        return await query_orders(drawing_no, db)

    return f"未识别指令：{content}\\n发送'帮助'查看可用命令。"


async def handle_click_event(user_id: str, key: str, db: AsyncSession):
    admin_acl_enabled = _has_admin_acl()
    is_admin = is_wechat_admin_user(user_id)
    is_basic = is_wechat_basic_user(user_id)

    # 每日报表仅管理员可用
    if key == "DAILY_REPORT" and admin_acl_enabled and not is_admin:
        return "当前账号无权限使用该功能。"

    # 库存查询普通用户也可使用
    if key == "INVENTORY_QUERY":
        return "请直接输入：库存 图号\n例如：库存 1M15E53603"

    # 待电镀入库和寄电镀出库在"半成品管理"菜单下，不限制权限

    if key == "DAILY_REPORT":
        return await get_daily_report(db)
    elif key == "PRODUCTION_PROGRESS":
        return await get_today_progress(db)
    elif key == "VIEW_ORDERS":
        return await get_incomplete_orders(db)
    elif key == "ADD_ORDER":
        if admin_acl_enabled and not is_admin:
            return "当前账号无权限使用该功能。"
        USER_SESSIONS[user_id] = {"state": "WAITING_FOR_ORDER_DATA"}
        return "请输入产品图号+PO+序号+数量\n例如：1M15E53603+260312+015+500"
    elif key == "REPORT_UPLOAD":
        USER_SESSIONS[user_id] = {"state": "WAITING_FOR_REPORT_DATA"}
        return (
            "操作指令：报表 机床+产品图号+PO+序号+数量+生产时间+工序\n"
            "示例：报表 1+1M15E53603+260310+26013+500+10+2"
        )
    elif key == "CURRENT_INVENTORY":
        return await get_top20_inventory(db)
    elif key == "PRODUCT_INBOUND":
        USER_SESSIONS[user_id] = {"state": "WAITING_FOR_INBOUND_DATA"}
        return (
            "操作指令：入库 产品图号+数量\n"
            "示例：入库 1M15E53603 500\n"
            "示例：入库 1M15E53603+500"
        )
    elif key == "INVENTORY_MODIFY":
        USER_SESSIONS[user_id] = {"state": "WAITING_FOR_INVENTORY_MODIFY"}
        return (
            "操作指令：修改 图号+数量\n"
            "说明：数量为修正后的实际库存值\n"
            "示例：修改 1M15E53603+500"
        )
    elif key == "PENDING_PLATING_INBOUND":
        USER_SESSIONS[user_id] = {"state": "WAITING_FOR_PENDING_PLATING_INBOUND_DATA"}
        return (
            "操作指令：待电镀 图号+数量\n"
            "示例：待电镀 1M15E53603+500"
        )
    elif key == "PLATING_OUTBOUND":
        USER_SESSIONS[user_id] = {"state": "WAITING_FOR_PLATING_OUTBOUND_DATA"}
        return (
            "操作指令：寄电镀 图号+数量\n"
            "说明：扣除仓库库存中相应的待电镀数量\n"
            "示例：寄电镀 1M15E53603+500"
        )
    elif key == "PENDING_PLATING_INVENTORY":
        return await get_pending_plating_inventory(db)
    elif key == "SEMI_FINISHED_INVENTORY":
        USER_SESSIONS[user_id] = {"state": "WAITING_FOR_SEMI_FINISHED_DATA"}
        return (
            "操作指令：半成品 图号+数量 / 图号-数量 / 图号 数量\n"
            "说明：+为加上，-为减去，空格为直接填写\n"
            "示例：半成品 1M15E53603+500\n"
            "示例：半成品 1M15E53603-500\n"
            "示例：半成品 1M15E53603 500"
        )
    elif key == "PRODUCT_OUTBOUND":
        USER_SESSIONS[user_id] = {"state": "WAITING_FOR_SHIPMENT_DATA"}
        return (
            "操作指令：出货 产品图号+PO+序号+数量\n"
            "示例：出货 1M15E53603+260310+26013+500"
        )

    return "未知操作"

# --- 具体业务逻辑实现 ---

async def get_daily_report(db: AsyncSession):
    """每日报表: 默认返回前一天数据（含时间和达标率）"""
    target_day = date.today() - timedelta(days=1)
    stmt = (
        select(ProductionLog)
        .where(func.date(ProductionLog.report_date) == target_day)
        .order_by(ProductionLog.machine_name.asc(), ProductionLog.process_name.asc(), ProductionLog.id.asc())
    )
    result = await db.execute(stmt)
    logs = result.scalars().all()

    if not logs:
        return f"📅 {target_day} 暂无报表数据。"

    product_ids = {int(log.product_id) for log in logs if log.product_id}
    drawings_without_product = {(log.drawing_no or "").strip() for log in logs if not log.product_id and log.drawing_no}
    products_by_id = {}
    products_by_drawing = {}
    if product_ids:
        prod_res = await db.execute(select(Product).where(Product.id.in_(product_ids)))
        for p in prod_res.scalars().all():
            products_by_id[p.id] = p
    if drawings_without_product:
        draw_res = await db.execute(select(Product).where(Product.drawing_no.in_(drawings_without_product)))
        for p in draw_res.scalars().all():
            if p.drawing_no:
                products_by_drawing[p.drawing_no.strip()] = p

    lines = [f"📊 {target_day} 生产日报（含时间/达标率）:"]
    for log in logs[:40]:
        product = products_by_id.get(log.product_id) if log.product_id else products_by_drawing.get((log.drawing_no or "").strip())
        standard_time = _get_process_standard_time(product, log.process_name)
        achievement_text = _calc_achievement_rate_text(standard_time, log.processing_time, log.quantity)

        lines.append(
            f"机床:{log.machine_name} | 图号:{log.drawing_no} | PO:{log.po_no or '-'} | 序:{normalize_seq_no(log.seq_no) or '-'}"
        )
        lines.append(
            f"工序:{log.process_name or '-'} | 数量:{int(log.quantity or 0)} | 时间(H):{log.processing_time or '-'} | 达标率:{achievement_text}"
        )

    remain = len(logs) - 40
    if remain > 0:
        lines.append(f"... 其余 {remain} 条请在系统报表页面查看")

    return "\n".join(lines)

async def get_today_progress(db: AsyncSession):
    """生产进度: 按图号+PO+序号显示各工序累计与当前做到哪道工序"""
    target_day = date.today() - timedelta(days=1)
    end_dt = datetime.combine(target_day, datetime.max.time())

    progress_stmt = (
        select(
            ProductionLog.drawing_no,
            ProductionLog.po_no,
            ProductionLog.seq_no,
            ProductionLog.process_name,
            func.sum(ProductionLog.quantity).label("total_qty"),
        )
        .where(ProductionLog.report_date <= end_dt)
        .group_by(
            ProductionLog.drawing_no,
            ProductionLog.po_no,
            ProductionLog.seq_no,
            ProductionLog.process_name,
        )
    )
    progress_res = await db.execute(progress_stmt)
    rows = progress_res.all()

    if not rows:
        return f"{target_day} 暂无生产进度记录。"

    order_stmt = (
        select(
            Product.drawing_no,
            Order.po_no,
            Order.seq_no,
            func.sum(Order.order_quantity).label("order_qty"),
        )
        .join(Product, Order.product_id == Product.id)
        .group_by(Product.drawing_no, Order.po_no, Order.seq_no)
    )
    order_res = await db.execute(order_stmt)
    order_qty_map = {}
    for row in order_res.all():
        key = (
            (row.drawing_no or "").strip(),
            normalize_po_no(row.po_no) or "-",
            normalize_seq_no(row.seq_no) or "-",
        )
        order_qty_map[key] = int(row.order_qty or 0)

    progress_map = defaultdict(dict)
    for row in rows:
        key = (
            (row.drawing_no or "").strip() or "未知",
            normalize_po_no(row.po_no) or "-",
            normalize_seq_no(row.seq_no) or "-",
        )
        process_name = (row.process_name or "-").strip() or "-"
        progress_map[key][process_name] = int(row.total_qty or 0)

    sorted_keys = sorted(
        progress_map.keys(),
        key=lambda k: (_sort_token(k[1]), _sort_token(k[2]), _sort_token(k[0])),
    )

    lines = [f"🚀 生产进度（截至 {target_day}）:"]
    for key in sorted_keys[:20]:
        draw, po, seq = key
        process_qty = progress_map[key]
        ordered_processes = sorted(process_qty.items(), key=lambda item: _process_sort_key(item[0]))
        current_process = ordered_processes[-1][0] if ordered_processes else "-"
        order_qty = int(order_qty_map.get(key, 0) or 0)

        lines.append(f"[图号:{draw} | PO:{po} | 序:{seq}]")
        lines.append(f"当前做到: {current_process}")

        proc_parts = []
        for proc, qty in ordered_processes:
            if order_qty > 0:
                pct = (qty / order_qty) * 100.0
                proc_parts.append(f"{proc}:{qty}({pct:.0f}%)")
            else:
                proc_parts.append(f"{proc}:{qty}")
        lines.append("工序累计: " + " ; ".join(proc_parts))

    remain = len(sorted_keys) - 20
    if remain > 0:
        lines.append(f"... 其余 {remain} 个 PO/序号进度请在系统页面查看")

    return "\n".join(lines)

async def get_incomplete_orders(db: AsyncSession):
    """查看订单: 获取未完成的订单"""
    stmt = select(Order, Product).join(Product, Order.product_id == Product.id).where(Order.status != 'completed').limit(20)
    result = await db.execute(stmt)
    rows = result.all()
    
    if not rows:
        return "✅ 目前没有未完成的订单。"
    
    lines = ["📋 未完成订单 (前20条):"]
    status_map = {"pending": "待处理", "producing": "生产中", "shipping": "待出货"}
    for order, product in rows:
        status_cn = status_map.get(order.status, order.status)
        lines.append(f"订单号(PO): {order.po_no or '-'} | 序号: {order.seq_no or '-'} | 图号: {product.drawing_no} | 数量: {order.order_quantity} | 状态: {status_cn}")
    return "\n".join(lines)

async def process_add_order(user_id: str, content: str, db: AsyncSession):
    """添加订单: 格式 1M15E53603+260312+015+500"""
    try:
        parts = content.split("+")
        if len(parts) != 4:
            return "格式错误！请输入 图号+po+序号+数量\n例如：1M15E53603+260312+015+500"
            
        drawing_no, po, seq, qty_str = parts
        qty = int(qty_str)
        
        # 查找产品
        res = await db.execute(select(Product).where(Product.drawing_no == drawing_no))
        product = res.scalar_one_or_none()
        
        if not product:
            # 自动创建产品
            product = Product(drawing_no=drawing_no, code=drawing_no, name=drawing_no)
            db.add(product)
            await db.flush()
        
        new_order = Order(
            order_no=f"ORD-{str(uuid.uuid4())[:8].upper()}",
            po_no=po,
            seq_no=seq,
            product_id=product.id,
            order_quantity=qty,
            status="pending"
        )
        db.add(new_order)
        await db.commit()

        # 与网页端保持一致：新增订单后通知管理员
        notify_msg = (
            "🧾 新增订单通知\n"
            f"图号: {drawing_no}\n"
            f"PO: {po or '-'}\n"
            f"序号: {seq or '-'}\n"
            f"数量: {qty}\n"
            f"操作人: {user_id or '-'}\n"
            "来源: 微信交互"
        )
        await send_wechat_notification(notify_msg)
        
        USER_SESSIONS.pop(user_id, None)
        return f"✅ 订单已成功录入！\n订单号(PO): {po}\n序号: {seq}\n图号: {drawing_no}\n数量: {qty}"
    except Exception as e:
        return f"❌ 录入失败: {str(e)}"


def _split_command_payload(content: str, command: str) -> list[str]:
    raw = (content or "").strip()
    if raw.startswith(command):
        raw = raw[len(command):].strip()

    raw = raw.replace("＋", "+").replace("，", ",")
    if "+" in raw:
        parts = [p.strip() for p in raw.split("+") if p.strip()]
    else:
        parts = [p.strip() for p in re.split(r"[\s,]+", raw) if p.strip()]
    return parts


async def process_stock_inbound(user_id: str, content: str, db: AsyncSession, from_session: bool = False):
    """微信入库: 入库 图号+数量 或 入库 图号 数量"""
    parts = _split_command_payload(content, "入库")
    if len(parts) != 2:
        return (
            "格式错误。\n"
            "操作指令：入库 产品图号+数量\n"
            "示例：入库 1M15E53603 500\n"
            "示例：入库 1M15E53603+500"
        )

    drawing_no, qty_str = parts
    try:
        qty = int(qty_str)
    except ValueError:
        return "数量必须是整数。"
    if qty <= 0:
        return "数量必须大于 0。"

    prod_res = await db.execute(select(Product).where(Product.drawing_no == drawing_no))
    product = prod_res.scalar_one_or_none()
    if not product:
        return f"未找到产品图号：{drawing_no}。请先在产品管理中建档。"

    inv_res = await db.execute(select(InventoryItem).where(InventoryItem.product_id == product.id))
    inv = inv_res.scalar_one_or_none()
    if not inv:
        inv = InventoryItem(product_id=product.id, quantity=0, pending_plating=0, warehouse="default", safety_stock=10)
        db.add(inv)
        await db.flush()

    before_qty = int(inv.quantity or 0)
    inv.quantity = before_qty + qty
    await db.commit()

    notify_msg = (
        "📥 产品入库通知\n"
        f"图号: {drawing_no}\n"
        f"入库数量: {qty}\n"
        f"库存: {before_qty} -> {inv.quantity}\n"
        f"操作人: {user_id or '-'}\n"
        "来源: 微信交互"
    )
    await send_wechat_notification(notify_msg)

    if from_session:
        USER_SESSIONS.pop(user_id, None)

    return (
        "✅ 入库成功\n"
        f"图号：{drawing_no}\n"
        f"入库数量：{qty}\n"
        f"库存变化：{before_qty} -> {inv.quantity}"
    )


async def process_pending_plating_inbound(
    user_id: str,
    content: str,
    db: AsyncSession,
    from_session: bool = False,
):
    """微信待电镀入库: 待电镀 图号+数量"""
    parts = _split_command_payload(content, "待电镀")
    if len(parts) != 2:
        return (
            "格式错误。\n"
            "操作指令：待电镀 图号+数量\n"
            "示例：待电镀 1M15E53603+500"
        )

    drawing_no, qty_str = parts
    try:
        qty = int(qty_str)
    except ValueError:
        return "数量必须是整数。"
    if qty <= 0:
        return "数量必须大于 0。"

    prod_res = await db.execute(select(Product).where(Product.drawing_no == drawing_no))
    product = prod_res.scalar_one_or_none()
    if not product:
        return f"未找到产品图号：{drawing_no}。请先在产品管理中建档。"

    inv_res = await db.execute(select(InventoryItem).where(InventoryItem.product_id == product.id))
    inv = inv_res.scalar_one_or_none()
    if not inv:
        inv = InventoryItem(product_id=product.id, quantity=0, pending_plating=0, warehouse="default", safety_stock=10)
        db.add(inv)
        await db.flush()

    before_pending = int(inv.pending_plating or 0)
    inv.pending_plating = before_pending + qty
    await db.commit()

    notify_msg = (
        "🧪 待电镀入库通知\n"
        f"图号: {drawing_no}\n"
        f"入库数量: {qty}\n"
        f"待电镀: {before_pending} -> {inv.pending_plating}\n"
        f"操作人: {user_id or '-'}\n"
        "来源: 微信交互"
    )
    await send_wechat_notification(notify_msg)

    if from_session:
        USER_SESSIONS.pop(user_id, None)

    return (
        "✅ 待电镀入库成功\n"
        f"图号：{drawing_no}\n"
        f"入库数量：{qty}\n"
        f"待电镀变化：{before_pending} -> {inv.pending_plating}"
    )


async def process_wechat_shipment(user_id: str, content: str, db: AsyncSession, from_session: bool = False):
    """微信出货: 出货 图号+PO+序号+数量"""
    parts = _split_command_payload(content, "出货")
    if len(parts) != 4:
        return (
            "格式错误。\n"
            "操作指令：出货 产品图号+PO+序号+数量\n"
            "示例：出货 1M15E53603+260310+26013+500"
        )

    drawing_no, po_raw, seq_raw, qty_str = parts
    try:
        qty = int(qty_str)
    except ValueError:
        return "数量必须是整数。"
    if qty <= 0:
        return "数量必须大于 0。"

    normalized_po = normalize_po_no(po_raw)
    normalized_seq = normalize_seq_no(seq_raw)
    if not normalized_po or not normalized_seq:
        return "PO 或序号格式无效，请检查后重试。"

    prod_res = await db.execute(select(Product).where(Product.drawing_no == drawing_no))
    product = prod_res.scalar_one_or_none()
    if not product:
        return f"未找到产品图号：{drawing_no}。"

    target_key = po_seq_tuple(normalized_po, normalized_seq)
    order_res = await db.execute(
        select(Order).where(Order.product_id == product.id).order_by(Order.updated_at.desc())
    )
    orders = order_res.scalars().all()
    order = next((o for o in orders if po_seq_tuple(o.po_no, o.seq_no) == target_key), None)
    if not order:
        return f"未找到匹配订单：图号 {drawing_no} / PO {normalized_po} / 序号 {normalized_seq}。"

    inv_res = await db.execute(select(InventoryItem).where(InventoryItem.product_id == product.id))
    inv = inv_res.scalar_one_or_none()
    if not inv:
        inv = InventoryItem(product_id=product.id, quantity=0, pending_plating=0, warehouse="default", safety_stock=10)
        db.add(inv)
        await db.flush()

    before_qty = int(inv.quantity or 0)
    inv.quantity = max(0, before_qty - qty)

    shipped_res = await db.execute(select(Shipment).where(Shipment.product_id == product.id))
    shipped_before = sum(
        int(s.quantity or 0)
        for s in shipped_res.scalars().all()
        if po_seq_tuple(s.po_no, s.seq_no) == target_key
    )
    shipped_after = shipped_before + qty

    new_shipment = Shipment(
        shipment_date=datetime.now(),
        po_no=normalized_po,
        seq_no=normalized_seq,
        product_id=product.id,
        quantity=qty,
        customer=product.category,
    )
    db.add(new_shipment)

    order.status = "completed" if shipped_after >= (order.order_quantity or 0) else "shipping"
    await db.commit()

    # 与 PC 端保持一致：出货后发送系统通知（仅推送给管理员列表）
    notify_msg = (
        "📦 微信出货录入通知\n"
        f"图号: {drawing_no}\n"
        f"PO: {normalized_po}\n"
        f"序号: {normalized_seq}\n"
        f"出货数量: {qty}\n"
        f"库存: {before_qty} -> {inv.quantity}\n"
        f"操作人: {user_id or '-'}\n"
        "来源: 微信交互"
    )
    await send_wechat_notification(notify_msg)

    if from_session:
        USER_SESSIONS.pop(user_id, None)

    pending_qty = max((order.order_quantity or 0) - shipped_after, 0)
    return (
        "✅ 出货记录已完成\n"
        f"图号：{drawing_no}\n"
        f"PO：{normalized_po}\n"
        f"序号：{normalized_seq}\n"
        f"出货数量：{qty}\n"
        f"库存变化：{before_qty} -> {inv.quantity}\n"
        f"订单剩余：{pending_qty}"
    )


def _normalize_machine_name(machine_raw: str) -> str:
    text = (machine_raw or "").strip().upper()
    if not text:
        return "M?"
    if text.startswith("M"):
        return text
    if text.isdigit():
        return f"M{text}"
    return text


def _normalize_process_name(process_raw: str) -> str:
    text = (process_raw or "").strip()
    if not text:
        return "-"
    if text.startswith("工序"):
        return text
    return f"工序{text}"


def _parse_float(value) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    match = re.search(r"[-+]?\d*\.?\d+", text)
    if not match:
        return None
    try:
        return float(match.group(0))
    except Exception:
        return None


def _extract_process_index(process_name: str) -> int | None:
    text = str(process_name or "").strip()
    if not text:
        return None
    match = re.search(r"(\d+)", text)
    if not match:
        return None
    return int(match.group(1))


def _get_process_standard_time(product: Product | None, process_name: str) -> float | None:
    if not product:
        return None
    idx = _extract_process_index(process_name)
    if idx is None or idx < 1 or idx > 8:
        return None
    raw = getattr(product, f"proc{idx}_time", None)
    if raw is None:
        return None
    try:
        return float(raw)
    except Exception:
        return None


def _calc_achievement_rate_text(
    standard_time: float | None,
    processing_time,
    quantity,
) -> str:
    if standard_time is None:
        return "-"
    proc_hours = _parse_float(processing_time)
    qty = float(quantity or 0)
    if proc_hours is None or proc_hours <= 0 or qty <= 0:
        return "-"
    actual_cycle_min = (60.0 * proc_hours) / qty
    if actual_cycle_min <= 0:
        return "-"
    rate = (standard_time / actual_cycle_min) * 100.0
    return f"{rate:.1f}%"


def _process_sort_key(process_name: str):
    idx = _extract_process_index(process_name)
    if idx is None:
        return (1, str(process_name or ""))
    return (0, idx)


def _sort_token(value: str):
    text = str(value or "").strip()
    if text.isdigit():
        return (0, int(text))
    return (1, text)


async def process_wechat_report_upload(user_id: str, content: str, db: AsyncSession, from_session: bool = False):
    """微信报表上传: 报表 机床+图号+PO+序号+数量+生产时间+工序"""
    parts = _split_command_payload(content, "报表")
    if len(parts) != 7:
        return (
            "格式错误。\n"
            "操作指令：报表 机床+产品图号+PO+序号+数量+生产时间+工序\n"
            "示例：报表 1+1M15E53603+260310+26013+500+10+2"
        )

    machine_raw, drawing_no, po_raw, seq_raw, qty_raw, proc_time_raw, process_raw = parts
    try:
        qty = int(qty_raw)
    except ValueError:
        return "生产数量必须是整数。"
    if qty <= 0:
        return "生产数量必须大于 0。"

    machine_name = _normalize_machine_name(machine_raw)
    process_name = _normalize_process_name(process_raw)
    normalized_po = normalize_po_no(po_raw)
    normalized_seq = normalize_seq_no(seq_raw)

    prod_res = await db.execute(select(Product).where(Product.drawing_no == drawing_no))
    product = prod_res.scalar_one_or_none()

    new_log = ProductionLog(
        report_date=datetime.now(),
        machine_name=machine_name,
        drawing_no=drawing_no,
        po_no=normalized_po,
        seq_no=normalized_seq,
        process_name=process_name,
        quantity=qty,
        processing_time=str(proc_time_raw),
        product_id=product.id if product else None,
    )
    db.add(new_log)
    await db.commit()

    notify_msg = (
        "📝 生产报表录入通知\n"
        f"机床: {machine_name}\n"
        f"图号: {drawing_no}\n"
        f"PO: {normalized_po or '-'}\n"
        f"序号: {normalized_seq or '-'}\n"
        f"工序: {process_name}\n"
        f"数量: {qty}\n"
        f"加工时间(H): {proc_time_raw}\n"
        f"操作人: {user_id or '-'}\n"
        "来源: 微信交互"
    )
    await send_wechat_notification(notify_msg)

    if from_session:
        USER_SESSIONS.pop(user_id, None)

    return (
        "✅ 报表上传成功\n"
        f"机床：{machine_name}\n"
        f"图号：{drawing_no}\n"
        f"PO：{normalized_po or '-'}\n"
        f"序号：{normalized_seq or '-'}\n"
        f"生产数量：{qty}\n"
        f"加工时间：{proc_time_raw}\n"
        f"工序：{process_name}"
    )

async def query_inventory(drawing_no: str, db: AsyncSession):
    """库存查询"""
    stmt = select(InventoryItem, Product).join(Product, InventoryItem.product_id == Product.id).where(Product.drawing_no == drawing_no)
    result = await db.execute(stmt)
    row = result.first()
    
    if not row:
        return f"🔍 未找到图号 [{drawing_no}] 的库存信息。"
    
    inv, prod = row
    return f"📦 库存信息 [{drawing_no}]:\n当前数量: {inv.quantity}\n待电镀: {inv.pending_plating}\n安全库存: {inv.safety_stock}"

async def query_progress(drawing_no: str, db: AsyncSession):
    """进度查询 - 显示当前生产中的PO/序号及各工序完成数量"""
    # 查找此图号所有生产记录，按PO+序号+工序汇总
    stmt = (
        select(
            ProductionLog.po_no,
            ProductionLog.seq_no,
            ProductionLog.process_name,
            func.sum(ProductionLog.quantity).label("total_qty"),
        )
        .where(ProductionLog.drawing_no == drawing_no)
        .group_by(ProductionLog.po_no, ProductionLog.seq_no, ProductionLog.process_name)
        .order_by(ProductionLog.po_no, ProductionLog.seq_no)
    )
    result = await db.execute(stmt)
    rows = result.all()

    if not rows:
        return f"🔍 未找到图号 [{drawing_no}] 的生产进度。"

    # 按PO+序号分组
    from collections import defaultdict
    progress_map = defaultdict(list)
    for row in rows:
        key = po_seq_tuple(row.po_no, row.seq_no)
        progress_map[key].append((row.process_name, int(row.total_qty or 0)))

    # 查找未完成的订单（当前在生产或待出货）
    order_stmt = (
        select(Order, Product)
        .join(Product, Order.product_id == Product.id)
        .where(and_(Product.drawing_no == drawing_no, Order.status.in_(["pending", "producing", "shipping"])))
    )
    order_res = await db.execute(order_stmt)
    active_orders = {}
    for order, product in order_res.all():
        key = po_seq_tuple(order.po_no, order.seq_no)
        active_orders[key] = order.order_quantity or 0

    lines = [f"📈 [{drawing_no}] 生产进度:"]
    sorted_keys = sorted(progress_map.keys(), key=lambda k: (_sort_token(k[0]), _sort_token(k[1])))

    for key in sorted_keys:
        po, seq = key
        processes = progress_map[key]
        is_active = "【进行中】" if key in active_orders else ""
        order_qty = active_orders.get(key, 0)

        lines.append(f"\nPO: {po} | 序: {seq} {is_active}")
        if order_qty > 0:
            lines.append(f"订单数量: {order_qty}")

        # 按工序排序
        sorted_procs = sorted(processes, key=lambda p: _process_sort_key(p[0]))
        for proc_name, qty in sorted_procs:
            lines.append(f"  {proc_name}: {qty} 件")

    return "\n".join(lines)

async def query_orders(drawing_no: str, db: AsyncSession):
    """订单查询 (未完成)"""
    stmt = select(Order, Product).join(Product, Order.product_id == Product.id)\
        .where(and_(Product.drawing_no == drawing_no, Order.status != 'completed'))
    result = await db.execute(stmt)
    rows = result.all()

    if not rows:
        return f"🔍 未找到图号 [{drawing_no}] 的未完成订单。"

    lines = [f"📋 [{drawing_no}] 未完成订单:"]
    status_map = {"pending": "待处理", "producing": "生产中", "shipping": "待出货"}

    # 查询已出货数量
    shipped_stmt = select(
        Shipment.po_no, Shipment.seq_no, func.sum(Shipment.quantity).label("shipped_qty")
    ).group_by(Shipment.po_no, Shipment.seq_no)
    shipped_res = await db.execute(shipped_stmt)
    shipped_map = {}
    for row in shipped_res.all():
        key = po_seq_tuple(row.po_no, row.seq_no)
        shipped_map[key] = int(row.shipped_qty or 0)

    for order, product in rows:
        status_cn = status_map.get(order.status, order.status)
        order_key = po_seq_tuple(order.po_no, order.seq_no)
        shipped_qty = shipped_map.get(order_key, 0)
        remaining_qty = max((order.order_quantity or 0) - shipped_qty, 0)
        lines.append(
            f"PO: {order.po_no} | 序: {order.seq_no} | 订单: {order.order_quantity} | "
            f"已出货: {shipped_qty} | 剩余: {remaining_qty} | 状态: {status_cn}"
        )
    return "\n".join(lines)

async def get_all_inventory(db: AsyncSession):
    """获取所有库存概览"""
    stmt = select(InventoryItem, Product).join(Product, InventoryItem.product_id == Product.id).limit(20)
    result = await db.execute(stmt)
    rows = result.all()
    
    if not rows:
        return "📭 目前没有任何库存数据。"

    lines = ["📦 库存概览 (前20条):"]
    for inv, prod in rows:
        lines.append(f"{prod.drawing_no}: {inv.quantity} PCS")
    return "\n".join(lines)


async def get_top20_inventory(db: AsyncSession):
    """获取库存数量最多的20个产品"""
    stmt = (
        select(InventoryItem, Product)
        .join(Product, InventoryItem.product_id == Product.id)
        .order_by(InventoryItem.quantity.desc())
        .limit(20)
    )
    result = await db.execute(stmt)
    rows = result.all()

    if not rows:
        return "📭 目前没有任何库存数据。"

    lines = ["📦 库存数量TOP20:"]
    for i, (inv, prod) in enumerate(rows, 1):
        lines.append(f"{i}. {prod.drawing_no}: {inv.quantity} PCS")
    return "\n".join(lines)


async def get_pending_plating_inventory(db: AsyncSession):
    """获取待电镀库存（数量大于0的产品）"""
    stmt = (
        select(InventoryItem, Product)
        .join(Product, InventoryItem.product_id == Product.id)
        .where(InventoryItem.pending_plating > 0)
        .order_by(InventoryItem.pending_plating.desc())
    )
    result = await db.execute(stmt)
    rows = result.all()

    if not rows:
        return "📭 目前没有待电镀的库存。"

    lines = ["📦 待电镀库存:"]
    for inv, prod in rows:
        lines.append(f"图号: {prod.drawing_no} | 待电镀: {inv.pending_plating} 件")
    return "\n".join(lines)


async def process_inventory_modify(user_id: str, content: str, db: AsyncSession, from_session: bool = False):
    """库存改修: 修改 图号+数量（数量为实际值）"""
    parts = _split_command_payload(content, "修改")
    if len(parts) != 2:
        return (
            "格式错误。\n"
            "操作指令：修改 图号+数量\n"
            "说明：数量为修正后的实际库存值\n"
            "示例：修改 1M15E53603+500"
        )

    drawing_no, qty_str = parts
    try:
        qty = int(qty_str)
    except ValueError:
        return "数量必须是整数。"

    prod_res = await db.execute(select(Product).where(Product.drawing_no == drawing_no))
    product = prod_res.scalar_one_or_none()
    if not product:
        return f"未找到产品图号：{drawing_no}。请先在产品管理中建档。"

    inv_res = await db.execute(select(InventoryItem).where(InventoryItem.product_id == product.id))
    inv = inv_res.scalar_one_or_none()
    if not inv:
        inv = InventoryItem(product_id=product.id, quantity=0, pending_plating=0, warehouse="default", safety_stock=10)
        db.add(inv)
        await db.flush()

    before_qty = int(inv.quantity or 0)
    inv.quantity = qty
    await db.commit()

    notify_msg = (
        "📝 库存改修通知\n"
        f"图号: {drawing_no}\n"
        f"旧库存: {before_qty}\n"
        f"新库存: {qty}\n"
        f"操作人: {user_id or '-'}\n"
        "来源: 微信交互"
    )
    await send_wechat_notification(notify_msg)

    if from_session:
        USER_SESSIONS.pop(user_id, None)

    return (
        f"✅ 库存改修成功\n"
        f"图号：{drawing_no}\n"
        f"旧库存：{before_qty}\n"
        f"新库存：{qty}"
    )


async def try_process_inventory_quick_modify(user_id: str, content: str, db: AsyncSession):
    """库存快捷修改：图号+数量(加) / 图号-数量(减) / 图号 数量(直接填)

    示例：
    - 1M15E53603+500  → 库存+500
    - 1M15E53603-500  → 库存-500
    - 1M15E53603 500  → 库存直接设为500
    """
    content = content.strip()
    if not content:
        return None

    # 排除已知的指令前缀
    known_prefixes = ("入库", "出货", "报表", "修改", "待电镀", "寄电镀", "库存", "进度", "订单", "帮助", "取消", "退出", "结束")
    if any(content.startswith(p) for p in known_prefixes):
        return None

    # 尝试匹配 图号+数量 / 图号-数量 格式
    match = re.match(r'^([A-Za-z0-9]+)([+-])(\d+)$', content)
    if match:
        drawing_no, operator, qty_str = match.groups()
        qty = int(qty_str)
        return await _do_inventory_adjust(user_id, drawing_no, qty, operator, db)

    # 尝试匹配 图号 数量（空格分隔，直接填）格式
    # 只有2部分，且第二部分是纯数字
    parts = content.split()
    if len(parts) == 2 and parts[1].isdigit():
        drawing_no, qty_str = parts
        qty = int(qty_str)
        return await _do_inventory_set(user_id, drawing_no, qty, db)

    return None


async def _do_inventory_adjust(user_id: str, drawing_no: str, qty: int, operator: str, db: AsyncSession):
    """执行库存加/减操作"""
    prod_res = await db.execute(select(Product).where(Product.drawing_no == drawing_no))
    product = prod_res.scalar_one_or_none()
    if not product:
        return f"未找到产品图号：{drawing_no}。请先在产品管理中建档。"

    inv_res = await db.execute(select(InventoryItem).where(InventoryItem.product_id == product.id))
    inv = inv_res.scalar_one_or_none()
    if not inv:
        inv = InventoryItem(product_id=product.id, quantity=0, pending_plating=0, warehouse="default", safety_stock=10)
        db.add(inv)
        await db.flush()

    before_qty = int(inv.quantity or 0)
    if operator == '+':
        inv.quantity = before_qty + qty
        op_text = "增加"
    else:
        inv.quantity = max(0, before_qty - qty)
        op_text = "减少"

    await db.commit()

    notify_msg = (
        "📝 库存调整通知\n"
        f"图号: {drawing_no}\n"
        f"操作: {op_text}\n"
        f"数量: {qty}\n"
        f"库存: {before_qty} -> {inv.quantity}\n"
        f"操作人: {user_id or '-'}\n"
        "来源: 微信交互"
    )
    await send_wechat_notification(notify_msg)

    return (
        f"✅ 库存调整成功\n"
        f"图号：{drawing_no}\n"
        f"{op_text}：{qty}\n"
        f"库存变化：{before_qty} -> {inv.quantity}"
    )


async def _do_inventory_set(user_id: str, drawing_no: str, qty: int, db: AsyncSession):
    """执行库存直接填写操作"""
    prod_res = await db.execute(select(Product).where(Product.drawing_no == drawing_no))
    product = prod_res.scalar_one_or_none()
    if not product:
        return f"未找到产品图号：{drawing_no}。请先在产品管理中建档。"

    inv_res = await db.execute(select(InventoryItem).where(InventoryItem.product_id == product.id))
    inv = inv_res.scalar_one_or_none()
    if not inv:
        inv = InventoryItem(product_id=product.id, quantity=0, pending_plating=0, warehouse="default", safety_stock=10)
        db.add(inv)
        await db.flush()

    before_qty = int(inv.quantity or 0)
    inv.quantity = qty
    await db.commit()

    notify_msg = (
        "📝 库存改修通知\n"
        f"图号: {drawing_no}\n"
        f"旧库存: {before_qty}\n"
        f"新库存: {qty}\n"
        f"操作人: {user_id or '-'}\n"
        "来源: 微信交互"
    )
    await send_wechat_notification(notify_msg)

    return (
        f"✅ 库存填写成功\n"
        f"图号：{drawing_no}\n"
        f"旧库存：{before_qty}\n"
        f"新库存：{qty}"
    )


async def process_plating_outbound(user_id: str, content: str, db: AsyncSession, from_session: bool = False):
    """寄电镀出库: 寄电镀 图号+数量（扣除待电镀数量）"""
    parts = _split_command_payload(content, "寄电镀")
    if len(parts) != 2:
        return (
            "格式错误。\n"
            "操作指令：寄电镀 图号+数量\n"
            "说明：扣除仓库库存中相应的待电镀数量\n"
            "示例：寄电镀 1M15E53603+500"
        )

    drawing_no, qty_str = parts
    try:
        qty = int(qty_str)
    except ValueError:
        return "数量必须是整数。"
    if qty <= 0:
        return "数量必须大于 0。"

    prod_res = await db.execute(select(Product).where(Product.drawing_no == drawing_no))
    product = prod_res.scalar_one_or_none()
    if not product:
        return f"未找到产品图号：{drawing_no}。请先在产品管理中建档。"

    inv_res = await db.execute(select(InventoryItem).where(InventoryItem.product_id == product.id))
    inv = inv_res.scalar_one_or_none()
    if not inv:
        return f"图号 {drawing_no} 没有库存记录。"

    before_pending = int(inv.pending_plating or 0)
    if before_pending < qty:
        return f"待电镀数量不足。当前待电镀：{before_pending}，申请出库：{qty}"

    inv.pending_plating = before_pending - qty
    await db.commit()

    notify_msg = (
        "📤 寄电镀出库通知\n"
        f"图号: {drawing_no}\n"
        f"出库数量: {qty}\n"
        f"待电镀: {before_pending} -> {inv.pending_plating}\n"
        f"操作人: {user_id or '-'}\n"
        "来源: 微信交互"
    )
    await send_wechat_notification(notify_msg)

    if from_session:
        USER_SESSIONS.pop(user_id, None)

    return (
        f"✅ 寄电镀出库成功\n"
        f"图号：{drawing_no}\n"
        f"出库数量：{qty}\n"
        f"待电镀变化：{before_pending} -> {inv.pending_plating}"
    )


async def process_semi_finished_inventory(user_id: str, content: str, db: AsyncSession, from_session: bool = False):
    """半成品库存: 半成品 图号+数量 / 图号-数量 / 图号 数量"""
    parts = _split_command_payload(content, "半成品")
    if len(parts) != 2:
        return (
            "格式错误。\n"
            "操作指令：半成品 图号+数量 / 图号-数量 / 图号 数量\n"
            "说明：+为加上，-为减去，空格为直接填写\n"
            "示例：半成品 1M15E53603+500\n"
            "示例：半成品 1M15E53603-500\n"
            "示例：半成品 1M15E53603 500"
        )

    drawing_no, qty_str = parts
    # 解析数量（可能是 +500, -500, 500）
    if qty_str.startswith(('+', '-')):
        operator = qty_str[0]
        qty_value = qty_str[1:]
        try:
            qty = int(qty_value)
        except ValueError:
            return "数量必须是整数。"
    else:
        operator = None
        try:
            qty = int(qty_str)
        except ValueError:
            return "数量必须是整数。"

    prod_res = await db.execute(select(Product).where(Product.drawing_no == drawing_no))
    product = prod_res.scalar_one_or_none()
    if not product:
        return f"未找到产品图号：{drawing_no}。请先在产品管理中建档。"

    inv_res = await db.execute(select(InventoryItem).where(InventoryItem.product_id == product.id))
    inv = inv_res.scalar_one_or_none()
    if not inv:
        inv = InventoryItem(product_id=product.id, quantity=0, pending_plating=0, warehouse="default", safety_stock=10)
        db.add(inv)
        await db.flush()

    before_qty = int(inv.pending_plating or 0)

    if operator == '+':
        inv.pending_plating = before_qty + qty
        op_text = "增加"
    elif operator == '-':
        inv.pending_plating = max(0, before_qty - qty)
        op_text = "减少"
    else:
        inv.pending_plating = qty
        op_text = "填写"

    await db.commit()

    notify_msg = (
        "🔧 半成品库存通知\n"
        f"图号: {drawing_no}\n"
        f"操作: {op_text}\n"
        f"数量: {qty}\n"
        f"半成品库存: {before_qty} -> {inv.pending_plating}\n"
        f"操作人: {user_id or '-'}\n"
        "来源: 微信交互"
    )
    await send_wechat_notification(notify_msg)

    if from_session:
        USER_SESSIONS.pop(user_id, None)

    return (
        f"✅ 半成品库存调整成功\n"
        f"图号：{drawing_no}\n"
        f"{op_text}：{qty}\n"
        f"半成品库存变化：{before_qty} -> {inv.pending_plating}"
    )

# --- 菜单创建实用功能 ---
@router.post("/setup-menu", summary="初始化自定义菜单")
async def setup_menu():
    """手动调用此 API 来设置微信菜单"""
    try:
        client = get_wechat_client()
        conf = load_wechat_config()
        menu_data = {
            "button": [
                {
                    "name": "生产管理",
                    "sub_button": [
                        {"type": "click", "name": "每日报表", "key": "DAILY_REPORT"},
                        {"type": "click", "name": "生产进度", "key": "PRODUCTION_PROGRESS"},
                        {"type": "click", "name": "查看订单", "key": "VIEW_ORDERS"},
                        {"type": "click", "name": "添加订单", "key": "ADD_ORDER"},
                        {"type": "click", "name": "报表上传", "key": "REPORT_UPLOAD"}
                    ]
                },
                {
                    "name": "库存管理",
                    "sub_button": [
                        {"type": "click", "name": "当前库存", "key": "CURRENT_INVENTORY"},
                        {"type": "click", "name": "库存查询", "key": "INVENTORY_QUERY"},
                        {"type": "click", "name": "产品入库", "key": "PRODUCT_INBOUND"},
                        {"type": "click", "name": "库存改修", "key": "INVENTORY_MODIFY"},
                        {"type": "click", "name": "产品出货", "key": "PRODUCT_OUTBOUND"}
                    ]
                },
                {
                    "name": "半成品管理",
                    "sub_button": [
                        {"type": "click", "name": "待电镀库存", "key": "PENDING_PLATING_INVENTORY"},
                        {"type": "click", "name": "待电镀入库", "key": "PENDING_PLATING_INBOUND"},
                        {"type": "click", "name": "寄电镀出库", "key": "PLATING_OUTBOUND"},
                        {"type": "click", "name": "半成品库存", "key": "SEMI_FINISHED_INVENTORY"}
                    ]
                }
            ]
        }
        client.menu.create(conf["agent_id"], menu_data)
        return {"code": 0, "msg": "菜单设置完成"}
    except Exception as e:
        return {"code": 1, "msg": f"菜单设置失败: {str(e)}"}


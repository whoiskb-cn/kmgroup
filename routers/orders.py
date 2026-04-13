# -*- coding: utf-8 -*-
from fastapi import APIRouter, Depends, Query, HTTPException, UploadFile, File
from fastapi.responses import StreamingResponse
import io
import re
import uuid
import urllib.parse
from typing import Optional

import pandas as pd
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from import_utils import read_upload_table
from models import InventoryItem, Order, Product, ProductionLog, Shipment
from product_service import ensure_products_by_drawing
from seq_utils import normalize_po_no, normalize_seq_no, po_seq_tuple
from wechat_runtime import send_wechat_notification

router = APIRouter(prefix="/orders", tags=["订单管理"])


class OrderCreate(BaseModel):
    drawing_no: str
    po_no: Optional[str] = None
    seq_no: Optional[str] = None
    order_quantity: int = 0


class OrderUpdate(BaseModel):
    po_no: Optional[str] = None
    seq_no: Optional[str] = None
    order_quantity: Optional[int] = None


def calc_material(order_quantity: int, can_produce: str) -> str:
    if not order_quantity or not can_produce:
        return "0.00 M"

    match = re.search(r"(\d+(\.\d+)?)", str(can_produce))
    if not match:
        return "0.00 M"

    capability = float(match.group(1))
    if capability <= 0:
        return "0.00 M"

    result = (order_quantity * 1.1) / capability * 2.5
    return f"{result:.2f} M"


def _first_non_empty(row, keys: list[str]):
    for key in keys:
        if key in row:
            value = row.get(key)
            if value is not None and str(value).strip() != "":
                return value
    return None


@router.get("/", summary="获取订单列表")
async def list_orders(
    q: str = Query("", description="搜索图号或 PO"),
    status: str = Query("all", description="过滤状态: all/completed/pending/producing"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(200, ge=1, le=1000, description="每页条数"),
    db: AsyncSession = Depends(get_db),
):
    # 先查询所有订单及其关联产品
    stmt = (
        select(Order, Product, InventoryItem.quantity.label("inventory_qty"))
        .outerjoin(Product, Order.product_id == Product.id)
        .outerjoin(InventoryItem, InventoryItem.product_id == Order.product_id)
    )
    count_stmt = select(func.count()).select_from(Order).outerjoin(Product, Order.product_id == Product.id)

    query_text = q.lower().strip()
    if query_text:
        text_filter = or_(
            Order.po_no.ilike(f"%{query_text}%"),
            Product.drawing_no.ilike(f"%{query_text}%"),
        )
        stmt = stmt.where(text_filter)
        count_stmt = count_stmt.where(text_filter)

    total = int((await db.execute(count_stmt)).scalar() or 0)
    stmt = stmt.order_by(Order.updated_at.desc()).offset((page - 1) * page_size).limit(page_size)

    rows = (await db.execute(stmt)).all()

    # 收集 product_id 用于批量查询
    order_product_ids = list(set(row[0].product_id for row in rows if row[0].product_id))

    # 批量查询出货记录（按产品+po+seq汇总）
    shipments_map = {}  # (product_id, po_no, seq_no) -> shipped_qty
    if order_product_ids:
        ship_stmt = (
            select(
                Shipment.product_id,
                func.coalesce(Shipment.po_no, "").label("po_no"),
                func.coalesce(Shipment.seq_no, "").label("seq_no"),
                func.sum(Shipment.quantity).label("shipped_qty"),
            )
            .where(Shipment.product_id.in_(order_product_ids))
            .group_by(
                Shipment.product_id,
                Shipment.po_no,
                Shipment.seq_no,
            )
        )
        ship_res = await db.execute(ship_stmt)
        for row in ship_res.all():
            shipments_map[(row.product_id, row.po_no, row.seq_no)] = int(row.shipped_qty or 0)

    # 批量查询生产记录
    prod_logs_exist = set()
    if order_product_ids:
        prod_stmt = (
            select(
                ProductionLog.product_id,
                func.coalesce(ProductionLog.po_no, "").label("po_no"),
                func.coalesce(ProductionLog.seq_no, "").label("seq_no"),
            )
            .where(ProductionLog.product_id.in_(order_product_ids))
            .group_by(
                ProductionLog.product_id,
                ProductionLog.po_no,
                ProductionLog.seq_no,
            )
        )
        prod_res = await db.execute(prod_stmt)
        for row in prod_res.all():
            prod_logs_exist.add((row.product_id, row.po_no, row.seq_no))

    response_list = []
    for order, product, inventory_qty in rows:
        order_qty = int(order.order_quantity or 0)

        norm_po = normalize_po_no(order.po_no) or ""
        norm_seq = normalize_seq_no(order.seq_no) or ""
        shipped_qty = shipments_map.get((order.product_id, norm_po, norm_seq), 0)
        has_production = (order.product_id, norm_po, norm_seq) in prod_logs_exist if order.product_id else False

        is_completed = order_qty > 0 and shipped_qty >= order_qty

        # 三种状态
        if is_completed:
            order_status = "订单已完成"
        elif has_production:
            order_status = "车间生产中"
        else:
            order_status = "订单待处理"

        # 按状态过滤
        if status != "all":
            if status == "completed" and order_status != "订单已完成":
                continue
            elif status == "pending" and order_status != "订单待处理":
                continue
            elif status == "producing" and order_status != "车间生产中":
                continue

        capability = product.can_produce_2_5m if product else ""
        response_list.append(
            {
                "id": order.id,
                "order_no": order.order_no,
                "po_no": order.po_no or "-",
                "seq_no": normalize_seq_no(order.seq_no) or "-",
                "order_quantity": order_qty,
                "drawing_no": product.drawing_no if product else "-",
                "material_spec": product.material_spec if product else "-",
                "can_produce": capability,
                "material_calc": calc_material(order_qty, capability),
                "inventory_qty": int(inventory_qty or 0),
                "shipped_qty": shipped_qty,
                "pending_qty": max(order_qty - shipped_qty, 0),
                "status": order_status,
                "has_production": has_production,
                "updated_at": order.updated_at.strftime("%Y-%m-%d %H:%M") if order.updated_at else "",
            }
        )

    return {
        "code": 0,
        "data": {
            "list": response_list,
            "pagination": {
                "page": page,
                "page_size": page_size,
                "total": total,
                "total_pages": (total + page_size - 1) // page_size if total else 0,
            },
        },
    }


@router.post("/", summary="新增订单")
async def create_order(data: OrderCreate, db: AsyncSession = Depends(get_db)):
    drawing_no = (data.drawing_no or "").strip()
    if not drawing_no:
        raise HTTPException(status_code=400, detail="产品图号不能为空")

    prod_res = await db.execute(select(Product).where(Product.drawing_no == drawing_no))
    prod = prod_res.scalars().first()
    if not prod:
        prod = Product(drawing_no=drawing_no, code=f"PROD_{drawing_no}")
        db.add(prod)
        await db.flush()

    normalized_po = normalize_po_no(data.po_no)
    normalized_seq = normalize_seq_no(data.seq_no)

    new_order = Order(
        order_no=f"ORD-{str(uuid.uuid4())[:8].upper()}",
        po_no=normalized_po,
        seq_no=normalized_seq,
        product_id=prod.id,
        order_quantity=int(data.order_quantity or 0),
    )
    db.add(new_order)
    await db.commit()

    msg = (
        "新增订单通知\n"
        f"图号: {drawing_no}\n"
        f"PO: {normalized_po or '-'}\n"
        f"序号: {normalized_seq or '-'}\n"
        f"数量: {new_order.order_quantity}\n"
        "系统已同步更新。"
    )
    await send_wechat_notification(msg)

    return {"code": 0, "message": "订单创建成功"}


@router.put("/{order_id}", summary="修改订单")
async def update_order(order_id: int, data: OrderUpdate, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Order).where(Order.id == order_id))
    order = result.scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="订单不存在")

    if data.po_no is not None:
        order.po_no = normalize_po_no(data.po_no)
    if data.seq_no is not None:
        order.seq_no = normalize_seq_no(data.seq_no)
    if data.order_quantity is not None:
        order.order_quantity = int(data.order_quantity or 0)

    await db.commit()
    return {"code": 0, "message": "订单更新成功"}


@router.delete("/{order_id}", summary="删除订单")
async def delete_order(order_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Order).where(Order.id == order_id))
    order = result.scalar_one_or_none()
    if order:
        await db.delete(order)
        await db.commit()
    return {"code": 0, "message": "删除成功"}


@router.post("/batch-import", summary="批量导入订单")
async def import_orders(file: UploadFile = File(...), db: AsyncSession = Depends(get_db)):
    try:
        df = await read_upload_table(file)
        if "产品图号" not in df.columns:
            raise HTTPException(status_code=400, detail="文件必须包含“产品图号”列")

        drawing_nos = {
            str(_first_non_empty(row, ["产品图号"]) or "").strip()
            for _, row in df.iterrows()
            if str(_first_non_empty(row, ["产品图号"]) or "").strip().lower() not in {"", "none", "nan"}
        }
        product_map = await ensure_products_by_drawing(db, drawing_nos)

        count = 0
        new_orders = []
        for _, row in df.iterrows():
            d_no = str(_first_non_empty(row, ["产品图号"]) or "").strip()
            if not d_no or d_no.lower() in ["none", "nan"]:
                continue

            po_raw = _first_non_empty(row, ["PO号", "PO", "po_no"])
            seq_raw = _first_non_empty(row, ["序号", "seq_no"])
            qty_raw = _first_non_empty(row, ["下单数量", "数量", "order_quantity"])

            po = normalize_po_no(po_raw)
            seq = normalize_seq_no(seq_raw)
            try:
                qty = int(qty_raw or 0)
            except Exception:
                qty = 0
            if qty <= 0:
                continue

            prod = product_map.get(d_no)
            if not prod:
                continue

            new_order = Order(
                order_no=f"ORD-{str(uuid.uuid4())[:8].upper()}",
                po_no=po,
                seq_no=seq,
                product_id=prod.id,
                order_quantity=qty,
            )
            db.add(new_order)
            new_orders.append((d_no, po, seq, qty))
            count += 1

        await db.commit()

        if new_orders:
            lines = ["批量订单导入通知\n系统已成功导入以下订单:"]
            for d_no, po, seq, qty in new_orders[:20]:
                lines.append(f"图号: {d_no} | PO: {po} | 序号: {seq} | 数量: {qty}")
            if len(new_orders) > 20:
                lines.append(f"...还有 {len(new_orders) - 20} 条记录")
            await send_wechat_notification("\n".join(lines))
        else:
            await send_wechat_notification(f"批量订单导入通知\n成功导入 {count} 条订单记录。")
        return {"code": 0, "msg": f"成功导入 {count} 条订单"}
    except HTTPException:
        await db.rollback()
        raise
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"导入失败: {str(e)}")


@router.get("/template", summary="下载订单导入模板")
async def get_template():
    columns = ["产品图号", "PO号", "序号", "下单数量"]
    example_data = [["1M15E53603", "PO20240311", "010", 1000]]
    df = pd.DataFrame(example_data, columns=columns)

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="订单导入模板")
    output.seek(0)

    filename = "订单管理批量导入模板.xlsx"
    encoded_filename = urllib.parse.quote(filename)
    headers = {
        "Content-Disposition": f"attachment; filename=\"{encoded_filename}\"; filename*=UTF-8''{encoded_filename}"
    }
    return StreamingResponse(
        output,
        headers=headers,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

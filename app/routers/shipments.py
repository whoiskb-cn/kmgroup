# -*- coding: utf-8 -*-
from datetime import datetime, timedelta
from typing import Optional
import io
import urllib.parse

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.import_utils import read_upload_table
from app.models import InventoryItem, Order, Product, Shipment
from app.product_service import ensure_products_by_drawing
from app.seq_utils import normalize_po_no, normalize_seq_no, po_seq_tuple
from app.wechat_runtime import send_wechat_notification

router = APIRouter(prefix="/shipments", tags=["出货记录"])


class ShipmentCreate(BaseModel):
    shipment_date: str
    drawing_no: str
    po_no: Optional[str] = None
    seq_no: Optional[str] = None
    quantity: int


def _first_non_empty(row, keys: list[str]):
    for key in keys:
        if key in row:
            value = row.get(key)
            if value is not None and str(value).strip() != "":
                return value
    return None


def _shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    month_index = year * 12 + (month - 1) + delta
    new_year = month_index // 12
    new_month = month_index % 12 + 1
    return new_year, new_month


@router.get("/", summary="获取出货列表")
async def list_shipments(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    drawing_no: Optional[str] = None,
    page: int = 1,
    page_size: int = 200,
    db: AsyncSession = Depends(get_db),
):
    query = select(Shipment, Product).outerjoin(Product, Shipment.product_id == Product.id)
    count_query = select(func.count()).select_from(Shipment).outerjoin(Product, Shipment.product_id == Product.id)

    if start_date:
        try:
            s_date = datetime.strptime(start_date, "%Y-%m-%d")
            query = query.where(Shipment.shipment_date >= s_date)
            count_query = count_query.where(Shipment.shipment_date >= s_date)
        except ValueError:
            pass

    if end_date:
        try:
            e_date = datetime.strptime(end_date, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
            query = query.where(Shipment.shipment_date <= e_date)
            count_query = count_query.where(Shipment.shipment_date <= e_date)
        except ValueError:
            pass

    if drawing_no:
        query = query.where(Product.drawing_no.ilike(f"%{drawing_no}%"))
        count_query = count_query.where(Product.drawing_no.ilike(f"%{drawing_no}%"))

    total = int((await db.execute(count_query)).scalar() or 0)
    query = query.order_by(Shipment.shipment_date.desc()).offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(query)
    rows = result.all()

    response_list = []
    total_quantity = 0
    for s, p in rows:
        total_quantity += int(s.quantity or 0)
        response_list.append(
            {
                "id": s.id,
                "shipment_date": s.shipment_date.strftime("%Y-%m-%d %H:%M"),
                "drawing_no": p.drawing_no if p else "-",
                "po_no": s.po_no or "-",
                "seq_no": normalize_seq_no(s.seq_no) or "-",
                "customer": s.customer or (p.category if p else "-"),
                "quantity": s.quantity,
            }
        )

    return {
        "code": 0,
        "data": {
            "list": response_list,
            "total_quantity": total_quantity,
            "pagination": {
                "page": page,
                "page_size": page_size,
                "total": total,
                "total_pages": (total + page_size - 1) // page_size if total else 0,
            },
        },
    }


@router.get("/stats", summary="获取出货统计（含本年度、本期、上期）")
async def get_shipment_stats(db: AsyncSession = Depends(get_db)):
    now = datetime.now()
    year_start = datetime(now.year, 1, 1, 0, 0, 0)
    year_end = datetime(now.year, 12, 31, 23, 59, 59)

    # 统计周期使用每月 25~次月24
    if now.day >= 25:
        current_start = datetime(now.year, now.month, 25, 0, 0, 0)
        end_year, end_month = _shift_month(now.year, now.month, 1)
        current_end = datetime(end_year, end_month, 24, 23, 59, 59)
    else:
        start_year, start_month = _shift_month(now.year, now.month, -1)
        current_start = datetime(start_year, start_month, 25, 0, 0, 0)
        current_end = datetime(now.year, now.month, 24, 23, 59, 59)

    last_start_year, last_start_month = _shift_month(current_start.year, current_start.month, -1)
    last_start = datetime(last_start_year, last_start_month, 25, 0, 0, 0)
    last_end = current_start - timedelta(seconds=1)

    current_res = await db.execute(
        select(func.sum(Shipment.quantity)).where(
            Shipment.shipment_date >= current_start, Shipment.shipment_date <= current_end
        )
    )
    current_period_total = int(current_res.scalar() or 0)

    last_res = await db.execute(
        select(func.sum(Shipment.quantity)).where(
            Shipment.shipment_date >= last_start, Shipment.shipment_date <= last_end
        )
    )
    last_period_total = int(last_res.scalar() or 0)

    year_res = await db.execute(
        select(func.sum(Shipment.quantity)).where(
            Shipment.shipment_date >= year_start, Shipment.shipment_date <= year_end
        )
    )
    current_year_total = int(year_res.scalar() or 0)

    return {
        "code": 0,
        "data": {
            "current_year_total": current_year_total,
            "current_year_start": year_start.strftime("%Y-%m-%d"),
            "current_year_end": year_end.strftime("%Y-%m-%d"),
            "current_period_total": current_period_total,
            "last_period_total": last_period_total,
            "current_period_start": current_start.strftime("%Y-%m-%d"),
            "current_period_end": current_end.strftime("%Y-%m-%d"),
            "last_period_start": last_start.strftime("%Y-%m-%d"),
            "last_period_end": last_end.strftime("%Y-%m-%d"),
            "current_month": current_period_total,
            "last_month": last_period_total,
        },
    }


@router.get("/orders-by-drawing", summary="根据图号获取可用 PO 和序号")
async def get_orders_by_drawing(drawing_no: str, db: AsyncSession = Depends(get_db)):
    if not drawing_no:
        return {"code": 0, "data": []}

    query = (
        select(Order.product_id, Order.po_no, Order.seq_no, func.sum(Order.order_quantity).label("total_ordered"))
        .join(Product, Order.product_id == Product.id)
        .where(Product.drawing_no == drawing_no.strip())
        .where(Order.po_no.isnot(None))
        .group_by(Order.product_id, Order.po_no, Order.seq_no)
    )
    res = await db.execute(query)
    orders = res.all()
    if not orders:
        return {"code": 0, "data": []}

    product_id = orders[0].product_id
    s_query = (
        select(Shipment.po_no, Shipment.seq_no, func.sum(Shipment.quantity).label("shipped_qty"))
        .where(Shipment.product_id == product_id)
        .group_by(Shipment.po_no, Shipment.seq_no)
    )
    s_res = await db.execute(s_query)
    shipments_agg = {}
    for row in s_res.all():
        shipments_agg[po_seq_tuple(row.po_no, row.seq_no)] = int(row.shipped_qty or 0)

    options = []
    for row in orders:
        if not row.po_no:
            continue
        key = po_seq_tuple(row.po_no, row.seq_no)
        shipped = shipments_agg.get(key, 0)
        if shipped < int(row.total_ordered or 0):
            options.append({"po_no": row.po_no, "seq_no": normalize_seq_no(row.seq_no) or ""})

    return {"code": 0, "data": options}


@router.post("/", summary="新增出货记录")
async def create_shipment(data: ShipmentCreate, db: AsyncSession = Depends(get_db)):
    drawing_no = (data.drawing_no or "").strip()
    if not drawing_no:
        raise HTTPException(status_code=400, detail="产品图号不能为空")
    if int(data.quantity or 0) <= 0:
        raise HTTPException(status_code=400, detail="出货数量必须大于 0")

    result = await db.execute(select(Product).where(Product.drawing_no == drawing_no))
    prod = result.scalars().first()
    if not prod:
        raise HTTPException(status_code=404, detail="产品库中未找到该图号，请先创建产品档案")

    s_date = datetime.now()
    raw_date = (data.shipment_date or "").replace("T", " ").strip()
    if raw_date:
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                s_date = datetime.strptime(raw_date, fmt)
                break
            except ValueError:
                continue

    normalized_po = normalize_po_no(data.po_no)
    normalized_seq = normalize_seq_no(data.seq_no)

    db.add(
        Shipment(
            shipment_date=s_date,
            po_no=normalized_po,
            seq_no=normalized_seq,
            product_id=prod.id,
            quantity=int(data.quantity),
            customer=prod.category,
        )
    )

    inv_res = await db.execute(select(InventoryItem).where(InventoryItem.product_id == prod.id))
    inv = inv_res.scalars().first()
    if inv:
        inv.quantity = max(0, int(inv.quantity or 0) - int(data.quantity))

    await db.commit()

    msg = (
        "出货通知\n"
        f"图号: {drawing_no}\n"
        f"PO: {normalized_po or '-'}\n"
        f"序号: {normalized_seq or '-'}\n"
        f"出货数量: {int(data.quantity)}\n"
        "当前库存已自动扣减。"
    )
    await send_wechat_notification(msg)

    return {"code": 0, "message": "出货记录已保存"}


@router.delete("/{shipment_id}", summary="删除出货记录")
async def delete_shipment(shipment_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Shipment).where(Shipment.id == shipment_id))
    shipment = result.scalars().first()
    if not shipment:
        raise HTTPException(status_code=404, detail="未找到该出货记录")

    inv_res = await db.execute(select(InventoryItem).where(InventoryItem.product_id == shipment.product_id))
    inv = inv_res.scalars().first()
    if inv:
        inv.quantity = int(inv.quantity or 0) + int(shipment.quantity or 0)

    await db.delete(shipment)
    await db.commit()
    return {"code": 0, "message": "记录已删除，库存已回退"}


@router.put("/{shipment_id}", summary="修改出货记录")
async def update_shipment(shipment_id: int, data: ShipmentCreate, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Shipment).where(Shipment.id == shipment_id))
    shipment = result.scalars().first()
    if not shipment:
        raise HTTPException(status_code=404, detail="未找到该出货记录")

    drawing_no = (data.drawing_no or "").strip()
    prod_res = await db.execute(select(Product).where(Product.drawing_no == drawing_no))
    prod = prod_res.scalars().first()
    if not prod:
        raise HTTPException(status_code=404, detail="产品库中未找到该图号")

    if shipment.product_id == prod.id:
        inv_res = await db.execute(select(InventoryItem).where(InventoryItem.product_id == prod.id))
        inv = inv_res.scalars().first()
        if inv:
            inv.quantity = max(
                0,
                int(inv.quantity or 0) + int(shipment.quantity or 0) - int(data.quantity or 0),
            )
    else:
        old_inv_res = await db.execute(select(InventoryItem).where(InventoryItem.product_id == shipment.product_id))
        old_inv = old_inv_res.scalars().first()
        if old_inv:
            old_inv.quantity = int(old_inv.quantity or 0) + int(shipment.quantity or 0)

        new_inv_res = await db.execute(select(InventoryItem).where(InventoryItem.product_id == prod.id))
        new_inv = new_inv_res.scalars().first()
        if new_inv:
            new_inv.quantity = max(0, int(new_inv.quantity or 0) - int(data.quantity or 0))

    s_date = shipment.shipment_date
    raw_date = (data.shipment_date or "").replace("T", " ").strip()
    if raw_date:
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                s_date = datetime.strptime(raw_date, fmt)
                break
            except ValueError:
                continue

    shipment.shipment_date = s_date
    shipment.po_no = normalize_po_no(data.po_no)
    shipment.seq_no = normalize_seq_no(data.seq_no)
    shipment.product_id = prod.id
    shipment.quantity = int(data.quantity or 0)
    shipment.customer = prod.category

    await db.commit()
    return {"code": 0, "message": "记录已更新"}


@router.post("/batch-import", summary="批量导入出货记录")
async def import_shipments(file: UploadFile = File(...), db: AsyncSession = Depends(get_db)):
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

        product_ids = [prod.id for prod in product_map.values() if getattr(prod, "id", None)]
        inventory_map = {}
        if product_ids:
            inv_res = await db.execute(select(InventoryItem).where(InventoryItem.product_id.in_(product_ids)))
            inventory_map = {inv.product_id: inv for inv in inv_res.scalars().all()}

        count = 0
        for _, row in df.iterrows():
            d_no = str(_first_non_empty(row, ["产品图号"]) or "").strip()
            if not d_no or d_no.lower() in ["none", "nan"]:
                continue

            po_raw = _first_non_empty(row, ["PO号", "PO", "po_no"])
            seq_raw = _first_non_empty(row, ["序号", "seq_no"])
            qty_raw = _first_non_empty(row, ["出货数量", "数量", "shipment_quantity"])
            date_raw = _first_non_empty(row, ["出货日期", "shipment_date", "日期"])

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

            try:
                s_date = pd.to_datetime(str(date_raw)).to_pydatetime() if date_raw else datetime.now()
            except Exception:
                s_date = datetime.now()

            db.add(
                Shipment(
                    shipment_date=s_date,
                    po_no=po,
                    seq_no=seq,
                    product_id=prod.id,
                    quantity=qty,
                    customer=prod.category,
                )
            )

            inv = inventory_map.get(prod.id)
            if inv:
                inv.quantity = max(0, int(inv.quantity or 0) - qty)
            else:
                inv = InventoryItem(product_id=prod.id, quantity=0)
                db.add(inv)
                inventory_map[prod.id] = inv

            count += 1

        await db.commit()
        await send_wechat_notification(
            f"批量出货导入通知\n系统已成功导入 {count} 条出货记录，库存已同步更新。"
        )
        return {"code": 0, "msg": f"成功导入 {count} 条出货记录"}
    except HTTPException:
        await db.rollback()
        raise
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"导入失败: {str(e)}")


@router.get("/template", summary="下载出货导入模板")
async def get_template():
    columns = ["出货日期", "产品图号", "PO号", "序号", "出货数量"]
    example_data = [[datetime.now().strftime("%Y-%m-%d"), "1M15E53603", "PO20240311", "010", 500]]
    df = pd.DataFrame(example_data, columns=columns)

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="出货导入模板")
    output.seek(0)

    filename = "出货管理批量导入模板.xlsx"
    encoded_filename = urllib.parse.quote(filename)
    headers = {
        "Content-Disposition": f"attachment; filename=\"{encoded_filename}\"; filename*=UTF-8''{encoded_filename}"
    }
    return StreamingResponse(
        output,
        headers=headers,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

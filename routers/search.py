# -*- coding: utf-8 -*-
from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.database import get_db
from app.models import Order, Product, InventoryItem, ProductionLog
from app.seq_utils import normalize_seq_no, po_seq_tuple

router = APIRouter(prefix="/search", tags=["进度查询"])

@router.get("/drawings", summary="搜索图号(自动补全)")
async def search_drawings(
    q: str = Query("", description="关键词"),
    db: AsyncSession = Depends(get_db)
):
    # 从 Product 表中搜索图号
    stmt = select(Product.drawing_no).distinct()
    if q:
        stmt = stmt.where(Product.drawing_no.ilike(f"%{q}%"))
    
    stmt = stmt.limit(20)
    result = await db.execute(stmt)
    drawings = result.scalars().all()
    # 过滤掉 None 
    drawings = [d for d in drawings if d]
    return {"code": 0, "data": drawings}

@router.get("/detail", summary="根据图号获取综合信息")
async def get_drawing_detail(
    drawing_no: str = Query(..., description="产品图号"),
    db: AsyncSession = Depends(get_db)
):
    # 1. 获取基本产品信息
    prod_stmt = select(Product).where(Product.drawing_no == drawing_no)
    prod_res = await db.execute(prod_stmt)
    product = prod_res.scalar_one_or_none()
    
    if not product:
        # 如果产品库没找到，可能只有报表或订单里有，我们先报错或者返回部分信息
        # 这里为了演示健壮性，我们允许继续查询
        pass

    # 2. 获取当前订单
    order_stmt = (
        select(Order)
        .join(Product, Order.product_id == Product.id)
        .where(Product.drawing_no == drawing_no)
    )
    order_res = await db.execute(order_stmt)
    orders = order_res.scalars().all()

    # 2.1 获取出货记录并按 (po_no, seq_no) 聚合
    from app.models import Shipment
    shipped_map = {}
    if product:
        ship_stmt = select(Shipment).where(Shipment.product_id == product.id)
        ship_res = await db.execute(ship_stmt)
        shipments = ship_res.scalars().all()
        for s in shipments:
            key = po_seq_tuple(s.po_no, s.seq_no)
            shipped_map[key] = shipped_map.get(key, 0) + (s.quantity or 0)
    
    # 3. 获取库存信息
    inventory_data = {"quantity": 0, "pending_plating": 0}
    if product:
        inv_stmt = select(InventoryItem).where(InventoryItem.product_id == product.id)
        inv_res = await db.execute(inv_stmt)
        inv_item = inv_res.scalar_one_or_none()
        if inv_item:
            inventory_data = {
                "quantity": inv_item.quantity,
                "pending_plating": inv_item.pending_plating,
                "safety_stock": inv_item.safety_stock
            }

    # 4. 获取生产进度 (聚合逻辑)
    # 建立订单映射用于计算目标基数
    order_qty_map = {}
    completed_order_keys = set()
    for o in orders:
        order_key = po_seq_tuple(o.po_no, o.seq_no)
        order_qty_map[order_key] = o.order_quantity
        shipped_qty = shipped_map.get(order_key, 0)
        if int(o.order_quantity or 0) > 0 and shipped_qty >= int(o.order_quantity or 0):
            completed_order_keys.add(order_key)
    
    log_stmt = select(ProductionLog).where(ProductionLog.drawing_no == drawing_no)
    log_res = await db.execute(log_stmt)
    logs = log_res.scalars().all()
    
    # 聚合 ProductionLog
    grouped = {}
    for log in logs:
        p_no, s_no = po_seq_tuple(log.po_no, log.seq_no)
        if (p_no, s_no) in completed_order_keys:
            continue
        proc = log.process_name or ""
        key = (p_no, s_no, proc)
        
        if key not in grouped:
            order_q = order_qty_map.get((p_no, s_no), 0)
            grouped[key] = {
                "po_no": p_no,
                "seq_no": s_no,
                "process_name": proc,
                "target_qty": int(order_q * 1.1),
                "total_qty": 0,
                "machines": set(),
                "daily_output_cap": 0.0
            }
        
        g = grouped[key]
        g["total_qty"] += log.quantity or 0
        if log.machine_name:
            g["machines"].add(log.machine_name.strip())
            
    # 计算进度百分比和状态
    production_list = []
    for k, v in grouped.items():
        v["machines"] = list(v["machines"])
        progress_pct = 0
        if v["target_qty"] > 0:
            progress_pct = min(100, int((v["total_qty"] / v["target_qty"]) * 100))
        
        production_list.append({
            "po_no": v["po_no"],
            "seq_no": v["seq_no"],
            "process_name": v["process_name"],
            "total_qty": v["total_qty"],
            "target_qty": v["target_qty"],
            "progress_pct": progress_pct,
            "machine_names": " / ".join(v["machines"])
        })

    return {
        "code": 0,
        "data": {
            "product": {
                "drawing_no": product.drawing_no if product else drawing_no,
                "name": product.name if product else "-",
                "material": product.material_spec if product else "-",
                "model_file": product.model_file if product else None
            },
            "orders": [{
                "po_no": o.po_no,
                "seq_no": normalize_seq_no(o.seq_no),
                "quantity": o.order_quantity,
                "shipped_qty": shipped_map.get(po_seq_tuple(o.po_no, o.seq_no), 0),
                "pending_shipment_qty": max(0, o.order_quantity - shipped_map.get(po_seq_tuple(o.po_no, o.seq_no), 0)),
                "status": o.status,
                "created_at": o.created_at.strftime("%Y-%m-%d")
            } for o in orders],
            "inventory": inventory_data,
            "production": production_list
        }
    }

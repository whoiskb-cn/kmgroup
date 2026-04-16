# -*- coding: utf-8 -*-
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models import Order, Product, ProductionLog, ProductionOrderState, ProductionProcessState
from seq_utils import po_seq_tuple

router = APIRouter(prefix="/production", tags=["生产进度"])


def _normalize_drawing_no(value: Optional[str]) -> str:
    text = (value or "").strip()
    return text or "未知图号"


def _normalize_process_name(value: Optional[str]) -> str:
    return (value or "").strip()


def _parse_processing_time(value: Optional[str]) -> float:
    digits = "".join([ch for ch in str(value or "") if ch.isdigit() or ch == "."])
    if not digits:
        return 0.0
    try:
        return float(digits)
    except Exception:
        return 0.0


class ProcessStateUpdate(BaseModel):
    drawing_no: str
    po_no: Optional[str] = None
    seq_no: Optional[str] = None
    process_name: Optional[str] = None
    is_completed: bool


class OrderStateUpdate(BaseModel):
    drawing_no: str
    po_no: Optional[str] = None
    seq_no: Optional[str] = None
    is_completed: bool


@router.get("/progress", summary="生产进度聚合")
async def get_progress(
    include_completed_orders: bool = Query(False, description="是否包含手动标记为已完工的订单"),
    drawing_no: Optional[str] = Query(None, description="按图号过滤"),
    po_no: Optional[str] = Query(None, description="按 PO 过滤"),
    seq_no: Optional[str] = Query(None, description="按序号过滤"),
    process_name: Optional[str] = Query(None, description="按工序过滤"),
    db: AsyncSession = Depends(get_db),
):
    drawing_filter = (drawing_no or "").strip() or None
    po_filter, seq_filter = po_seq_tuple(po_no, seq_no)
    po_filter = po_filter or None
    seq_filter = seq_filter or None
    process_filter = _normalize_process_name(process_name) or None

    order_stmt = (
        select(Product.drawing_no, Order.po_no, Order.seq_no, Order.order_quantity)
        .join(Product, Order.product_id == Product.id, isouter=True)
    )
    if drawing_filter:
        order_stmt = order_stmt.where(Product.drawing_no == drawing_filter)
    if po_filter:
        order_stmt = order_stmt.where(Order.po_no == po_filter)
    if seq_filter:
        order_stmt = order_stmt.where(Order.seq_no == seq_filter)

    process_state_stmt = select(
        ProductionProcessState.drawing_no,
        ProductionProcessState.po_no,
        ProductionProcessState.seq_no,
        ProductionProcessState.process_name,
        ProductionProcessState.is_completed,
    )
    if drawing_filter:
        process_state_stmt = process_state_stmt.where(ProductionProcessState.drawing_no == drawing_filter)
    if po_filter:
        process_state_stmt = process_state_stmt.where(ProductionProcessState.po_no == po_filter)
    if seq_filter:
        process_state_stmt = process_state_stmt.where(ProductionProcessState.seq_no == seq_filter)
    if process_filter:
        process_state_stmt = process_state_stmt.where(ProductionProcessState.process_name == process_filter)

    order_state_stmt = select(
        ProductionOrderState.drawing_no,
        ProductionOrderState.po_no,
        ProductionOrderState.seq_no,
        ProductionOrderState.is_completed,
    )
    if drawing_filter:
        order_state_stmt = order_state_stmt.where(ProductionOrderState.drawing_no == drawing_filter)
    if po_filter:
        order_state_stmt = order_state_stmt.where(ProductionOrderState.po_no == po_filter)
    if seq_filter:
        order_state_stmt = order_state_stmt.where(ProductionOrderState.seq_no == seq_filter)

    log_stmt = select(
        ProductionLog.drawing_no,
        ProductionLog.po_no,
        ProductionLog.seq_no,
        ProductionLog.process_name,
        ProductionLog.machine_name,
        ProductionLog.quantity,
        ProductionLog.processing_time,
        ProductionLog.report_date,
    )
    if drawing_filter:
        log_stmt = log_stmt.where(ProductionLog.drawing_no == drawing_filter)
    if po_filter:
        log_stmt = log_stmt.where(ProductionLog.po_no == po_filter)
    if seq_filter:
        log_stmt = log_stmt.where(ProductionLog.seq_no == seq_filter)
    if process_filter:
        log_stmt = log_stmt.where(ProductionLog.process_name == process_filter)

    # 顺序执行所有查询（保持稳定）
    order_res = await db.execute(order_stmt)
    process_state_res = await db.execute(process_state_stmt)
    order_state_res = await db.execute(order_state_stmt)
    log_res = await db.execute(log_stmt)

    order_qty_map: dict[tuple[str, str, str], int] = {}
    for drawing_value, po_value, seq_value, order_quantity in order_res.all():
        key = (_normalize_drawing_no(drawing_value), *po_seq_tuple(po_value, seq_value))
        order_qty_map[key] = order_qty_map.get(key, 0) + int(order_quantity or 0)

    process_state_map = {
        (_normalize_drawing_no(d_no), *po_seq_tuple(po, seq), _normalize_process_name(proc)): bool(is_completed)
        for d_no, po, seq, proc, is_completed in process_state_res.all()
    }

    order_state_map = {
        (_normalize_drawing_no(d_no), *po_seq_tuple(po, seq)): bool(is_completed)
        for d_no, po, seq, is_completed in order_state_res.all()
    }
    grouped: dict[tuple[str, str, str, str], dict] = {}

    for drawing_value, po_value, seq_value, process_value, machine_value, quantity, processing_time, report_date in log_res.all():
        d_no = _normalize_drawing_no(drawing_value)
        p_no, s_no = po_seq_tuple(po_value, seq_value)
        proc = _normalize_process_name(process_value)
        order_key = (d_no, p_no, s_no)
        order_manual_completed = bool(order_state_map.get(order_key, False))

        if order_manual_completed and not include_completed_orders:
            continue

        key = (d_no, p_no, s_no, proc)
        if key not in grouped:
            order_qty = int(order_qty_map.get((d_no, p_no, s_no), 0) or 0)
            grouped[key] = {
                "drawing_no": d_no,
                "po_no": p_no,
                "seq_no": s_no,
                "process_name": proc,
                "order_qty": order_qty,
                "target_qty": int(order_qty * 1.1),
                "total_qty": 0,
                "machines_data": {},
                "machine_daily_qty": {},
                "process_manual_completed": bool(process_state_map.get(key, False)),
                "order_manual_completed": order_manual_completed,
            }

        group = grouped[key]
        qty = int(quantity or 0)
        group["total_qty"] += qty

        machine_name = (machine_value or "未知机床").strip() or "未知机床"
        machine_data = group["machines_data"].setdefault(machine_name, {"qty": 0, "time": 0.0})
        machine_data["qty"] += qty
        machine_data["time"] += _parse_processing_time(processing_time)

        report_day = report_date.strftime("%Y-%m-%d") if report_date else "未知日期"
        daily_map = group["machine_daily_qty"].setdefault(machine_name, {})
        daily_map[report_day] = daily_map.get(report_day, 0) + qty

    results = []
    for value in grouped.values():
        total_daily_output = 0.0
        for machine_data in value["machines_data"].values():
            if machine_data["time"] > 0:
                hourly_rate = machine_data["qty"] / machine_data["time"]
                total_daily_output += hourly_rate * 10.0

        eta = "-"
        eta_days = -1.0
        if value["process_manual_completed"]:
            eta = "手动完成"
            eta_days = 0.0
        elif total_daily_output > 0 and value["target_qty"] > 0:
            remaining_qty = max(0, value["target_qty"] - value["total_qty"])
            if remaining_qty == 0:
                eta = "已完成"
                eta_days = 0.0
            else:
                eta_days = remaining_qty / total_daily_output
                eta = f"{eta_days:.1f} 天"
        elif value["target_qty"] == 0:
            eta = "无订单基数"
        else:
            eta = "进度不足以评估"

        progress_pct = 0
        if value["target_qty"] > 0:
            progress_pct = min(100, int((value["total_qty"] / value["target_qty"]) * 100))

        machine_daily_output = []
        for machine_name, daily_map in value["machine_daily_qty"].items():
            day_items = []
            machine_total = 0
            for day, day_qty in sorted(daily_map.items(), reverse=True):
                qty_int = int(day_qty or 0)
                machine_total += qty_int
                day_items.append({"date": day, "quantity": qty_int})
            machine_daily_output.append(
                {
                    "machine_name": machine_name,
                    "total_qty": machine_total,
                    "days": day_items,
                }
            )

        results.append(
            {
                "drawing_no": value["drawing_no"],
                "po_no": value["po_no"],
                "seq_no": value["seq_no"],
                "process_name": value["process_name"],
                "machine_names": " / ".join(sorted(value["machines_data"].keys())),
                "total_qty": value["total_qty"],
                "order_qty": value["order_qty"],
                "target_qty": value["target_qty"],
                "progress_pct": progress_pct,
                "eta": eta,
                "eta_days": eta_days,
                "machine_daily_output": machine_daily_output,
                "process_manual_completed": bool(value["process_manual_completed"]),
                "order_manual_completed": bool(value["order_manual_completed"]),
            }
        )

    results.sort(
        key=lambda item: (
            str(item.get("drawing_no") or ""),
            str(item.get("po_no") or ""),
            str(item.get("seq_no") or ""),
            str(item.get("process_name") or ""),
        )
    )
    return {"code": 0, "data": {"list": results}}


@router.put("/process-state", summary="更新工序手动完成状态")
async def update_process_state(data: ProcessStateUpdate, db: AsyncSession = Depends(get_db)):
    drawing_no = _normalize_drawing_no(data.drawing_no)
    if not drawing_no:
        raise HTTPException(status_code=400, detail="drawing_no 不能为空")

    po_no, seq_no = po_seq_tuple(data.po_no, data.seq_no)
    process_name = _normalize_process_name(data.process_name)

    stmt = select(ProductionProcessState).where(
        ProductionProcessState.drawing_no == drawing_no,
        ProductionProcessState.po_no == po_no,
        ProductionProcessState.seq_no == seq_no,
        ProductionProcessState.process_name == process_name,
    )
    result = await db.execute(stmt)
    state = result.scalar_one_or_none()

    if state is None:
        state = ProductionProcessState(
            drawing_no=drawing_no,
            po_no=po_no,
            seq_no=seq_no,
            process_name=process_name,
        )
        db.add(state)

    state.is_completed = bool(data.is_completed)
    state.completed_at = datetime.now() if state.is_completed else None
    await db.commit()
    return {"code": 0, "message": "工序状态已更新"}


@router.put("/order-state", summary="更新订单手动完成状态")
async def update_order_state(data: OrderStateUpdate, db: AsyncSession = Depends(get_db)):
    drawing_no = _normalize_drawing_no(data.drawing_no)
    if not drawing_no:
        raise HTTPException(status_code=400, detail="drawing_no 不能为空")

    po_no, seq_no = po_seq_tuple(data.po_no, data.seq_no)
    stmt = select(ProductionOrderState).where(
        ProductionOrderState.drawing_no == drawing_no,
        ProductionOrderState.po_no == po_no,
        ProductionOrderState.seq_no == seq_no,
    )
    result = await db.execute(stmt)
    state = result.scalar_one_or_none()

    if state is None:
        state = ProductionOrderState(
            drawing_no=drawing_no,
            po_no=po_no,
            seq_no=seq_no,
        )
        db.add(state)

    state.is_completed = bool(data.is_completed)
    state.completed_at = datetime.now() if state.is_completed else None
    await db.commit()
    return {"code": 0, "message": "订单状态已更新"}

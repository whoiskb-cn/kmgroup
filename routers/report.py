# -*- coding: utf-8 -*-
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Query
from fastapi.responses import StreamingResponse
import pandas as pd
import io
import urllib.parse
import re
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, or_, and_, desc, func
from sqlalchemy.orm import selectinload
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
from database import get_db
from import_utils import read_upload_table
from models import ProductionLog, Product, Order
from product_service import ensure_products_by_drawing
from seq_utils import normalize_seq_no, normalize_po_no
from wechat_runtime import send_wechat_notification

router = APIRouter(prefix="/report", tags=["鏁版嵁鎶ヨ〃"])


class ProductionLogCreate(BaseModel):
    report_date: str
    machine_name: str
    drawing_no: str
    po_no: Optional[str] = ""
    seq_no: Optional[str] = ""
    process_name: str
    quantity: int
    processing_time: Optional[str] = "10"


def _normalize_processing_time(value: Optional[str]) -> str:
    normalized = str(value).strip() if value is not None else ""
    return normalized or "10"


def _normalize_machine_name(value: Optional[str]) -> str:
    normalized = str(value).strip() if value is not None else ""
    if not normalized:
        return "M"
    if normalized.upper().startswith("M"):
        suffix = normalized[1:].strip()
        return f"M{suffix}" if suffix else "M"
    return f"M{normalized}"


def _normalize_drawing_key(value: Optional[str]) -> str:
    return re.sub(r"\s+", "", str(value or "").strip()).lower()


def _parse_float(value) -> Optional[float]:
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


def _get_process_standard_time(product: Optional[Product], process_name: Optional[str]) -> Optional[float]:
    if not product:
        return None
    text = str(process_name or "").strip()
    match = re.search(r"(\d+)", text)
    if not match:
        return None
    idx = int(match.group(1))
    if idx < 1 or idx > 8:
        return None
    raw = getattr(product, f"proc{idx}_time", None)
    if raw is None:
        return None
    try:
        return float(raw)
    except Exception:
        return None


class ProductionLogSchema(BaseModel):
    id: int
    report_date: datetime
    machine_name: str
    drawing_no: str
    po_no: Optional[str]
    seq_no: Optional[str]
    process_name: str
    quantity: int
    model_file: Optional[str] = None

    class Config:
        from_attributes = True


@router.get("/logs", summary="鑾峰彇鐢熶骇璁板綍鍒楄〃")
async def list_production_logs(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    q: Optional[str] = None,
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(200, ge=1, le=1000, description="每页条数"),
    db: AsyncSession = Depends(get_db),
):
    filters = []
    if start_date:
        filters.append(ProductionLog.report_date >= datetime.strptime(start_date, "%Y-%m-%d"))
    if end_date:
        filters.append(
            ProductionLog.report_date <= datetime.strptime(end_date + " 23:59:59", "%Y-%m-%d %H:%M:%S")
        )
    if q:
        filters.append(
            or_(
                ProductionLog.drawing_no.ilike(f"%{q}%"),
                ProductionLog.po_no.ilike(f"%{q}%"),
                ProductionLog.machine_name.ilike(f"%{q}%"),
            )
        )

    count_stmt = select(func.count()).select_from(ProductionLog)
    if filters:
        count_stmt = count_stmt.where(*filters)
    total = int((await db.execute(count_stmt)).scalar() or 0)

    stmt = (
        select(ProductionLog)
        .options(selectinload(ProductionLog.product))
        .order_by(desc(ProductionLog.report_date), ProductionLog.machine_name.asc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    if filters:
        stmt = stmt.where(*filters)

    result = await db.execute(stmt)
    logs = result.scalars().all()

    res_list = []
    for log in logs:
        standard_time = _get_process_standard_time(log.product, log.process_name)
        processing_hours = _parse_float(log.processing_time)
        qty = float(log.quantity or 0)
        actual_cycle_min = None
        if processing_hours and processing_hours > 0 and qty > 0:
            actual_cycle_min = (60.0 * processing_hours) / qty

        achievement_rate = None
        if standard_time is not None and actual_cycle_min is not None and actual_cycle_min > 0:
            achievement_rate = round((standard_time / actual_cycle_min) * 100, 1)

        res_list.append(
            {
                "id": log.id,
                "report_date": log.report_date.strftime("%Y-%m-%d"),
                "machine_name": log.machine_name,
                "drawing_no": log.drawing_no,
                "po_no": log.po_no,
                "seq_no": normalize_seq_no(log.seq_no),
                "process_name": log.process_name,
                "quantity": log.quantity,
                "processing_time": log.processing_time,
                "model_file": log.product.model_file if log.product else None,
                "standard_time": round(standard_time, 2) if standard_time is not None else None,
                "actual_cycle_min": round(actual_cycle_min, 2) if actual_cycle_min is not None else None,
                "achievement_rate": achievement_rate,
            }
        )

    return {
        "code": 0,
        "data": {
            "list": res_list,
            "pagination": {
                "page": page,
                "page_size": page_size,
                "total": total,
                "total_pages": (total + page_size - 1) // page_size if total else 0,
            },
        },
    }


@router.post("/logs", summary="鏂板鐢熶骇璁板綍")
async def create_production_log(data: ProductionLogCreate, db: AsyncSession = Depends(get_db)):
    # 鏌ユ壘鍏宠仈浜у搧
    prod_stmt = select(Product).where(Product.drawing_no == data.drawing_no)
    prod_res = await db.execute(prod_stmt)
    product = prod_res.scalar_one_or_none()

    new_log = ProductionLog(
        report_date=datetime.strptime(data.report_date, "%Y-%m-%d"),
        machine_name=_normalize_machine_name(data.machine_name),
        drawing_no=data.drawing_no,
        po_no=data.po_no,
        seq_no=data.seq_no,
        process_name=data.process_name,
        quantity=data.quantity,
        processing_time=_normalize_processing_time(data.processing_time),
        product_id=product.id if product else None,
    )

    db.add(new_log)
    await db.commit()

    # 妫€鏌ュ綋鍓嶅伐搴忔槸鍚﹁揪鍒?110%
    await check_production_finished(data.drawing_no, data.po_no, data.seq_no, data.process_name, db)

    return {"code": 0, "message": "璁板綍鍒涘缓鎴愬姛"}


async def check_production_finished(drawing_no, po_no, seq_no, process_name, db: AsyncSession):
    """Check whether one process reaches 110% and send a notification."""
    try:
        normalized_key = _normalize_drawing_key(drawing_no)
        order = None

        if normalized_key:
            strict_stmt = (
                select(Order)
                .join(Product, Product.id == Order.product_id)
                .where(
                    and_(
                        Order.po_no == po_no,
                        Order.seq_no == seq_no,
                        func.replace(func.lower(func.trim(Product.drawing_no)), " ", "") == normalized_key,
                    )
                )
            )
            strict_res = await db.execute(strict_stmt)
            order = strict_res.scalar_one_or_none()

        if order is None:
            fallback_stmt = select(Order).where(and_(Order.po_no == po_no, Order.seq_no == seq_no))
            fallback_res = await db.execute(fallback_stmt)
            fallback_orders = fallback_res.scalars().all()
            if len(fallback_orders) == 1:
                order = fallback_orders[0]

        if not order or order.order_quantity <= 0:
            return

        target_qty = int(order.order_quantity * 1.1)

        # 获取当前累计进度
        log_stmt = select(func.sum(ProductionLog.quantity)).where(
            and_(
                ProductionLog.drawing_no == drawing_no,
                ProductionLog.po_no == po_no,
                ProductionLog.seq_no == seq_no,
                ProductionLog.process_name == process_name,
            )
        )
        log_res = await db.execute(log_stmt)
        current_total = log_res.scalar() or 0

        if current_total >= target_qty:
            msg = (
                "生产进度完成通知\n"
                f"图号: {drawing_no}\n"
                f"工序: {process_name}\n"
                f"PO: {po_no or '-'}\n"
                f"序号: {seq_no or '-'}\n"
                f"状态: 已达到 110% 目标 (目标 {target_qty} / 累计 {current_total})\n"
                "请关注后续工序安排。"
            )
            await send_wechat_notification(msg)
    except Exception as e:
        import logging

        logging.getLogger("Report").error(f"检查进度通知失败: {e}")


@router.put("/logs/{log_id}", summary="淇敼鐢熶骇璁板綍")
async def update_production_log(log_id: int, data: ProductionLogCreate, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(ProductionLog).where(ProductionLog.id == log_id))
    log = result.scalar_one_or_none()
    if not log:
        raise HTTPException(status_code=404, detail="记录不存在")

    prod_stmt = select(Product).where(Product.drawing_no == data.drawing_no)
    prod_res = await db.execute(prod_stmt)
    product = prod_res.scalar_one_or_none()

    log.report_date = datetime.strptime(data.report_date, "%Y-%m-%d")
    log.machine_name = _normalize_machine_name(data.machine_name)
    log.drawing_no = data.drawing_no
    log.po_no = data.po_no
    log.seq_no = data.seq_no
    log.process_name = data.process_name
    log.quantity = data.quantity
    log.processing_time = _normalize_processing_time(data.processing_time)
    log.product_id = product.id if product else None

    await db.commit()
    return {"code": 0, "message": "鏇存柊鎴愬姛"}


@router.delete("/logs/{log_id}", summary="鍒犻櫎鐢熶骇璁板綍")
async def delete_production_log(log_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(ProductionLog).where(ProductionLog.id == log_id))
    log = result.scalar_one_or_none()
    if log:
        await db.delete(log)
        await db.commit()
    return {"code": 0, "message": "鍒犻櫎鎴愬姛"}


@router.get("/orders-by-drawing", summary="鏍规嵁鍥惧彿鑾峰彇 PO 淇℃伅")
async def get_orders_by_drawing(drawing_no: str, db: AsyncSession = Depends(get_db)):
    normalized_drawing = (drawing_no or "").strip()
    if not normalized_drawing:
        return {"code": 0, "data": []}

    normalized_key = _normalize_drawing_key(normalized_drawing)
    stmt = select(Order).join(Product).where(
        func.replace(func.lower(func.trim(Product.drawing_no)), " ", "") == normalized_key
    )
    result = await db.execute(stmt)
    orders = result.scalars().all()

    # 去重并返回
    unique_pos = []
    seen = set()
    for o in orders:
        normalized_seq = normalize_seq_no(o.seq_no)
        key = (o.po_no, normalized_seq)
        if key not in seen:
            unique_pos.append({"po_no": o.po_no, "seq_no": normalized_seq, "order_quantity": o.order_quantity})
            seen.add(key)

    # 兜底：将该图号在生产日志中出现过的 PO/序号也补充进来
    log_stmt = select(ProductionLog.po_no, ProductionLog.seq_no).where(
        func.replace(func.lower(func.trim(ProductionLog.drawing_no)), " ", "") == normalized_key
    )
    log_res = await db.execute(log_stmt)
    log_rows = log_res.all()
    for po_no, seq_no in log_rows:
        normalized_seq = normalize_seq_no(seq_no)
        key = (po_no, normalized_seq)
        if key not in seen:
            unique_pos.append({"po_no": po_no, "seq_no": normalized_seq, "order_quantity": None})
            seen.add(key)

    return {"code": 0, "data": unique_pos}


@router.get("/process-progress", summary="鑾峰彇宸ュ簭绱瀹屾垚鏁伴噺")
async def get_process_progress(
    drawing_no: str = Query(..., description="鍥惧彿"),
    process_name: str = Query(..., description="宸ュ簭鍚嶇О"),
    po_no: Optional[str] = Query(None, description="PO"),
    seq_no: Optional[str] = Query(None, description="搴忓彿"),
    db: AsyncSession = Depends(get_db),
):
    drawing = (drawing_no or "").strip()
    process = (process_name or "").strip()
    if not drawing or not process:
        raise HTTPException(status_code=400, detail="drawing_no and process_name are required")

    po = normalize_po_no(po_no)
    seq = normalize_seq_no(seq_no)

    qty_stmt = select(func.sum(ProductionLog.quantity)).where(
        ProductionLog.drawing_no == drawing,
        ProductionLog.process_name == process,
    )
    if po:
        qty_stmt = qty_stmt.where(ProductionLog.po_no == po)
    else:
        qty_stmt = qty_stmt.where(or_(ProductionLog.po_no.is_(None), ProductionLog.po_no == ""))

    if seq:
        qty_stmt = qty_stmt.where(ProductionLog.seq_no == seq)
    else:
        qty_stmt = qty_stmt.where(or_(ProductionLog.seq_no.is_(None), ProductionLog.seq_no == ""))

    qty_res = await db.execute(qty_stmt)
    total_qty = int(qty_res.scalar() or 0)

    order_stmt = (
        select(func.sum(Order.order_quantity))
        .select_from(Order)
        .join(Product, Product.id == Order.product_id)
        .where(Product.drawing_no == drawing)
    )
    if po:
        order_stmt = order_stmt.where(Order.po_no == po)
    else:
        order_stmt = order_stmt.where(or_(Order.po_no.is_(None), Order.po_no == ""))

    if seq:
        order_stmt = order_stmt.where(Order.seq_no == seq)
    else:
        order_stmt = order_stmt.where(or_(Order.seq_no.is_(None), Order.seq_no == ""))

    order_res = await db.execute(order_stmt)
    order_qty_raw = order_res.scalar()
    order_qty = int(order_qty_raw) if order_qty_raw else 0
    target_qty = int(order_qty * 1.1) if order_qty > 0 else 0
    progress_pct = int(min(100, (total_qty / target_qty) * 100)) if target_qty > 0 else 0

    return {
        "code": 0,
        "data": {
            "drawing_no": drawing,
            "po_no": po or "",
            "seq_no": seq or "",
            "process_name": process,
            "total_qty": total_qty,
            "order_qty": order_qty,
            "target_qty": target_qty,
            "progress_pct": progress_pct,
        },
    }


@router.post("/batch-import", summary="鎵归噺瀵煎叆鐢熶骇璁板綍")
async def import_production_logs(file: UploadFile = File(...), db: AsyncSession = Depends(get_db)):
    try:
        df = await read_upload_table(file)

        def _norm_col_name(name: str) -> str:
            text = str(name or "").strip().lower()
            for ch in (" ", "_", "-", "（", "）", "(", ")", "：", ":", "\t", "\n", "\r"):
                text = text.replace(ch, "")
            return text

        alias_map = {
            "report_date": ["报告日期", "报表日期", "日期", "report_date", "鎶ュ憡鏃ユ湡"],
            "machine_name": ["机床名称", "机床", "machine_name", "鏈哄簥鍚嶇О"],
            "drawing_no": ["产品图号", "图号", "drawing_no", "浜у搧鍥惧彿"],
            "process_name": ["工序名称", "工序", "process_name", "宸ュ簭鍚嶇О"],
            "quantity": ["生产数量", "数量", "quantity", "qty", "鐢熶骇鏁伴噺"],
            "processing_time": ["加工时间", "加工时间h", "加工时间(H)", "processing_time", "鍔犲伐鏃堕棿"],
            "po_no": ["PO号", "PO", "po_no", "PO鍙?"],
            "seq_no": ["序号", "seq_no", "搴忓彿"],
        }

        normalized_cols = {_norm_col_name(col): col for col in df.columns}

        def _resolve_col(key: str, required: bool = False):
            for alias in alias_map.get(key, []):
                real_col = normalized_cols.get(_norm_col_name(alias))
                if real_col is not None:
                    return real_col
            if required:
                readable = " / ".join(alias_map.get(key, []))
                raise HTTPException(status_code=400, detail=f"缺少必要列: {readable}。当前列: {list(df.columns)}")
            return None

        col_report_date = _resolve_col("report_date", required=True)
        col_machine_name = _resolve_col("machine_name", required=True)
        col_drawing_no = _resolve_col("drawing_no", required=True)
        col_process_name = _resolve_col("process_name", required=True)
        col_quantity = _resolve_col("quantity", required=True)
        col_processing_time = _resolve_col("processing_time", required=False)
        col_po_no = _resolve_col("po_no", required=False)
        col_seq_no = _resolve_col("seq_no", required=False)

        drawing_nos = {
            str(row.get(col_drawing_no, "")).strip()
            for _, row in df.iterrows()
            if str(row.get(col_drawing_no, "")).strip().lower() not in {"", "none", "nan"}
        }
        product_map = await ensure_products_by_drawing(db, drawing_nos)

        count = 0
        for _, row in df.iterrows():
            d_no = str(row.get(col_drawing_no, "")).strip()
            if not d_no or d_no.lower() in ["none", "nan", ""]:
                continue

            machine = str(row.get(col_machine_name, "M")).strip()
            if machine.lower() in ["none", "nan", ""]:
                machine = "M"
            machine = _normalize_machine_name(machine)

            proc = str(row.get(col_process_name, "加工")).strip()
            if proc.lower() in ["none", "nan", ""]:
                proc = "加工"

            try:
                qty = int(row.get(col_quantity, 0) or 0)
            except Exception:
                qty = 0

            p_time = row.get(col_processing_time) if col_processing_time else ""
            p_time = str(p_time).strip() if p_time is not None else ""
            if p_time.lower() in ["none", "nan"]:
                p_time = ""

            po = row.get(col_po_no) if col_po_no else None
            po = str(po).strip() if po is not None else None
            if po and po.lower() in ["none", "nan"]:
                po = None
            po = normalize_po_no(po)

            seq = row.get(col_seq_no) if col_seq_no else None
            seq = str(seq).strip() if seq is not None else None
            if seq and seq.lower() in ["none", "nan"]:
                seq = None
            seq = normalize_seq_no(seq)

            date_str = str(row.get(col_report_date, "")).strip()

            if qty <= 0:
                continue

            prod = product_map.get(d_no)
            if not prod:
                continue

            # 瑙ｆ瀽鏃ユ湡
            try:
                r_date = pd.to_datetime(date_str).to_pydatetime()
            except Exception:
                r_date = datetime.now()

            # 鍒涘缓璁板綍
            new_log = ProductionLog(
                report_date=r_date,
                machine_name=machine,
                drawing_no=d_no,
                po_no=po,
                seq_no=seq,
                process_name=proc,
                quantity=qty,
                processing_time=p_time,
                product_id=prod.id,
            )
            db.add(new_log)
            count += 1

        await db.commit()

        # 鎵归噺瀵煎叆閫氱煡
        await send_wechat_notification(f"批量生产记录导入\n系统已成功导入 {count} 条生产记录。")

        return {"code": 0, "msg": f"成功导入 {count} 条生产记录"}
    except HTTPException:
        await db.rollback()
        raise
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"瀵煎叆澶辫触: {str(e)}")


@router.get("/template", summary="涓嬭浇鐢熶骇璁板綍瀵煎叆妯℃澘")
async def get_template():
    columns = ["报告日期", "机床名称", "产品图号", "PO号", "序号", "工序名称", "生产数量", "加工时间"]
    example_data = [[datetime.now().strftime("%Y-%m-%d"), "M1", "1M15E53603", "PO20240311", "10", "粗加工", 100, "15.5"]]
    df = pd.DataFrame(example_data, columns=columns)

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="生产记录导入模板")
    output.seek(0)

    filename = "报表汇总批量添加模板.xlsx"
    encoded_filename = urllib.parse.quote(filename)
    headers = {"Content-Disposition": f"attachment; filename=\"{encoded_filename}\"; filename*=UTF-8''{encoded_filename}"}
    return StreamingResponse(
        output,
        headers=headers,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

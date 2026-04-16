# -*- coding: utf-8 -*-
from datetime import datetime, timedelta
import math
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, delete, func, union
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models import Order, Product, ProductionLog, ScheduleAssignment

router = APIRouter(prefix="/schedule", tags=["订单排产"])

CHINESE_HOLIDAYS = {
    '2026-01-01': '元旦',
    '2026-04-04': '清明节',
    '2026-04-05': '清明节',
    '2026-04-06': '清明节',
    '2026-05-01': '劳动节',
    '2026-05-02': '劳动节',
    '2026-05-03': '劳动节',
    '2026-05-04': '劳动节',
    '2026-05-05': '劳动节',
    '2026-06-19': '端午节',
    '2026-09-25': '中秋节',
    '2026-09-26': '中秋节',
    '2026-09-27': '中秋节',
    '2026-10-01': '国庆节',
    '2026-10-02': '国庆节',
    '2026-10-03': '国庆节',
    '2026-10-04': '国庆节',
    '2026-10-05': '国庆节',
    '2026-10-06': '国庆节',
    '2026-10-07': '国庆节',
}

def is_resting_day(dt: datetime) -> bool:
    ds = dt.strftime("%Y-%m-%d")
    return dt.weekday() == 6 or ds in CHINESE_HOLIDAYS

def _calc_completion_days(quantity: int, proc_time_minutes: float, work_hours: float = 15.0) -> float:
    if quantity <= 0 or not proc_time_minutes or proc_time_minutes <= 0: return 0.0
    daily_cap = (float(work_hours) * 60.0) / float(proc_time_minutes)
    return float(quantity) / float(daily_cap) if daily_cap > 0 else 99.0

def _add_working_days(start_date: datetime, days_needed: float) -> datetime:
    curr = start_date
    rem = float(days_needed)
    safety = 0
    while rem > 0.001 and safety < 300:
        safety += 1
        if is_resting_day(curr):
            curr = (curr + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            continue
        if rem >= 1.0:
            curr += timedelta(days=1); rem -= 1.0
        else:
            curr += timedelta(hours=rem * 10.0); rem = 0
    return curr

def _normalize_proc(process_name: str) -> str:
    """Normalize process name: '工序5' -> '5', '5' -> '5'"""
    if not process_name:
        return ""
    import re
    m = re.search(r'\d+', process_name)
    return m.group(0) if m else process_name

@router.get("/gantt", summary="极致动态排产链")
async def get_gantt(
    days_ahead: int = Query(120),
    db: AsyncSession = Depends(get_db),
):
    today_dt = (datetime.now() - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)

    # 只从排产单获取机床名，不从报表获取
    q_ms_1 = select(ScheduleAssignment.machine_name).where(ScheduleAssignment.machine_name != "")
    all_ms = (await db.execute(q_ms_1)).scalars().all()

    machine_chains = {m: [] for m in all_ms if m}

    plans = (await db.execute(select(ScheduleAssignment).where(ScheduleAssignment.is_completed == False))).scalars().all()

    po_load = {}
    # 保存每个排产记录的原始开始日期
    plan_start_dates = {}
    # 建立 lookup: (drawing, po, seq, proc, machine) -> ScheduleAssignment
    sa_lookup = {}
    # 统计每个任务的总分配数量和并行机器数量
    task_total_assigned = {}  # (drawing, po, seq, proc) -> total_assigned
    task_machine_count = {}   # (drawing, po, seq, proc) -> machine_count
    task_assigned_values = {}  # (drawing, po, seq, proc) -> set of assigned_quantities
    for p in plans:
        norm_proc = _normalize_proc(p.process_name)
        key = (p.drawing_no, p.po_no or "", p.seq_no or "", norm_proc, p.machine_name or "")
        sa_lookup[key] = p
        task_key = (p.drawing_no, p.po_no or "", p.seq_no or "", norm_proc)
        task_machine_count[task_key] = task_machine_count.get(task_key, 0) + 1
        task_total_assigned[task_key] = task_total_assigned.get(task_key, 0) + (p.assigned_quantity or 0)
        if p.assigned_quantity:
            if task_key not in task_assigned_values:
                task_assigned_values[task_key] = set()
            task_assigned_values[task_key].add(p.assigned_quantity)

    def reg_load(d, po, s, pr, m, pt, start_date=None):
        key = (d, po or "", s or "", pr)
        if key not in po_load: po_load[key] = {"machines": set(), "proc_time": pt or 0.0}
        po_load[key]["machines"].add(m)
        if pt and pt > 0: po_load[key]["proc_time"] = pt
        # 保存原始开始日期
        if start_date:
            plan_start_dates[key] = start_date

    for p in plans:
        reg_load(p.drawing_no, p.po_no, p.seq_no, _normalize_proc(p.process_name), p.machine_name, p.proc_time_minutes, p.start_date)

    finished_res = await db.execute(
        select(
            ProductionLog.drawing_no,
            ProductionLog.po_no,
            ProductionLog.seq_no,
            ProductionLog.process_name,
            ProductionLog.machine_name,
            func.sum(ProductionLog.quantity).label("finished_qty"),
        ).group_by(
            ProductionLog.drawing_no,
            ProductionLog.po_no,
            ProductionLog.seq_no,
            ProductionLog.process_name,
            ProductionLog.machine_name,
        )
    )
    finished_map = {}
    process_finished = {}
    process_machines = {}
    for drawing_no, po_no, seq_no, process_name, machine_name, finished_qty in finished_res.all():
        proc_key = (drawing_no or "", po_no or "", seq_no or "", process_name or "")
        process_finished[proc_key] = process_finished.get(proc_key, 0) + int(finished_qty or 0)
        finished_map[(machine_name or "", drawing_no or "", po_no or "", seq_no or "", process_name or "")] = int(finished_qty or 0)
        if machine_name:
            process_machines.setdefault(proc_key, set()).add(machine_name)

    for key, info in po_load.items():
        drawing, po, seq, proc = key
        total_p = int(process_finished.get((drawing, po, seq, proc), 0) or 0)

        order_qty = (await db.execute(
            select(func.sum(Order.order_quantity))
            .join(Product, Order.product_id == Product.id, isouter=True)
            .where(Order.po_no == po, Order.seq_no == seq, Product.drawing_no == drawing)
        )).scalar() or 0

        # 计算当前工序的完成数量（不是所有工序的总和）
        # total_p 已经是当前 proc 的已完成数量
        target_qty = int(order_qty * 1.1) if order_qty > 0 else 0

        plan_q = (await db.execute(select(func.sum(ScheduleAssignment.assigned_quantity)).where(
            ScheduleAssignment.drawing_no == drawing, ScheduleAssignment.po_no == po, ScheduleAssignment.seq_no == seq, ScheduleAssignment.process_name == proc,
            ScheduleAssignment.is_completed == False
        ))).scalar() or 0

        if target_qty <= 0:
            target_qty = int(plan_q) if plan_q > 0 else max(total_p + 1, 1)

        # 使用当前工序完成数量来计算剩余（不是所有工序的总和）
        remaining = max(0, order_qty - total_p)
        # 优先使用 info["machines"]（来自 ScheduleAssignment），如果为空才用 process_machines
        machines = sorted(info["machines"]) if info["machines"] else sorted(process_machines.get((drawing, po, seq, proc), []))
        share_map = {}
        if machines:
            if remaining <= len(machines):
                for idx, m in enumerate(machines):
                    share_map[m] = 1 if idx < remaining else 0
            else:
                base = remaining // len(machines)
                extra = remaining % len(machines)
                for idx, m in enumerate(machines):
                    share_map[m] = base + (1 if idx < extra else 0)

        if not machines and total_p > 0:
            machines = ["未知机床"]

        for m in machines:
            if m not in machine_chains:
                machine_chains[m] = []
            share = share_map.get(m, 0)
            machine_finished = finished_map.get((m or "", drawing, po or "", seq or "", proc), 0)
            if share == 0 and machine_finished > 0:
                share = int(machine_finished)
            if share == 0 and total_p > 0:
                share = 1
            if share == 0 and total_p == 0 and plan_q == 0:
                continue

            display_target = max(int(share), int(machine_finished), 1)

            # 获取这条排产记录的数据库ID和work_hours
            sa_record = sa_lookup.get((drawing, po, seq, proc, m))
            task_key = (drawing, po, seq, proc)
            machine_count = task_machine_count.get(task_key, 1)
            total_assigned = task_total_assigned.get(task_key, 0)

            # 计算总剩余数量
            task_remaining = max(0, remaining)

            # 基础目标：使用当前剩余计算的值
            base_target = max(int(share), int(machine_finished), 1)

            # 如果有多台机器并行且总分配超过剩余，需要调整
            # 逻辑：新任务保持输入值，旧任务减少
            display_target = base_target
            if machine_count > 1 and sa_record and sa_record.assigned_quantity:
                if total_assigned > task_remaining and task_remaining > 0:
                    # 检查分配值是否全相同
                    all_same = len(task_assigned_values.get(task_key, set())) == 1
                    # 按 ID 排序，最小的是第一个（最旧的）
                    same_task_sas = sorted([(k, v) for k, v in sa_lookup.items() if k[:4] == (drawing, po, seq, proc)], key=lambda x: x[1].id if x[1].id else 0)
                    first_sa = same_task_sas[0] if same_task_sas else None
                    is_first = first_sa and (drawing, po, seq, proc, m) == first_sa[0]
                    if all_same:
                        # 所有分配相同，平均分配剩余
                        display_target = max(1, task_remaining // machine_count)
                    elif is_first:
                        # 第一个（最旧的）任务承担减少量
                        # 其他任务的分配总和
                        other_total = sum(int(v.assigned_quantity) for k, v in same_task_sas if k != (drawing, po, seq, proc, m))
                        display_target = max(1, task_remaining - other_total)
                    else:
                        # 非第一个任务保持原值（用户输入）
                        display_target = int(sa_record.assigned_quantity)

            # 注意：不要在这里更新 assigned_quantity 到数据库！
            # gantt API 只负责计算和显示 display_target，不应修改用户的实际分配数量

            remaining_qty = max(0, display_target - int(machine_finished))
            progress_pct = min(100, int(round(100.0 * int(machine_finished) / float(display_target))))

            work_hours = float(sa_record.work_hours) if sa_record and sa_record.work_hours else 15.0
            days = _calc_completion_days(display_target, info["proc_time"], work_hours)
            machine_chains[m].append({
                "id": sa_record.id if sa_record else f"t_{drawing}_{m}_{proc}",
                "drawing_no": drawing, "po_no": po, "seq_no": seq, "process_name": proc,
                "assigned_quantity": int(display_target),  # 使用重新计算后的值
                "work_hours": work_hours,
                "finished_quantity": int(machine_finished),
                "target_quantity": int(display_target), "remaining_quantity": int(remaining_qty),
                "progress_pct": progress_pct, "days_needed": days, "actual_work_days": days, "parallel_count": len(machines),
                "parallel_info": (f"{len(machines)}机并行" if len(machines) > 1 else ""),
                "is_actual": False,  # 不再从 ProductionLog 读取
                "order_finished_qty": int(process_finished.get((drawing, po, seq, proc), 0)),  # 订单维度已完成数量
            })

    final_list = []
    machine_occupancy = {}
    for m, chain in machine_chains.items():
        chain.sort(key=lambda x: (not x["is_actual"], x["id"]))
        cursor = today_dt
        for bar in chain:
            # 优先使用原始排产的开始日期，否则用计算的值
            key = (bar.get("drawing_no", ""), bar.get("po_no", ""), bar.get("seq_no", ""), bar.get("process_name", ""))
            orig_start = plan_start_dates.get(key)
            if orig_start:
                bar["start_date"] = orig_start
            elif bar["is_actual"] or bar == chain[0]:
                bar["start_date"] = today_dt
            else:
                bar["start_date"] = cursor
            bar["end_date"] = _add_working_days(bar["start_date"], bar["days_needed"])
            cursor = bar["end_date"]
            it = {**bar, "machine_name": m}
            it["start_date"] = it["start_date"].strftime("%Y-%m-%d"); it["end_date"] = it["end_date"].strftime("%Y-%m-%d")
            final_list.append(it)
        machine_occupancy[m] = cursor.strftime("%Y-%m-%d")

    return {"code": 0, "data": {
        "list": final_list, "machines": sorted(list(machine_chains.keys()), key=lambda x: (x[0] if x else '', x[1:] if len(x)>1 else '')),
        "machine_occupancy": machine_occupancy,
        "date_range": {"start": today_dt.strftime("%Y-%m-%d"), "end": (today_dt + timedelta(days=days_ahead)).strftime("%Y-%m-%d")}
    }}


@router.get("/holidays", summary="获取节日列表")
async def get_holidays():
    return {"code": 0, "data": CHINESE_HOLIDAYS}


@router.get("/calculate-end-date", summary="计算工作日结束日期")
async def calculate_end_date(
    start_date: str = Query(..., description="开始日期 YYYY-MM-DD"),
    quantity: int = Query(..., description="数量"),
    proc_time_minutes: float = Query(..., description="工序时间(分钟/件)"),
    work_hours: float = Query(10.0, description="每日生产时长(小时)"),
):
    """计算考虑周日和法定节假日的工作日结束日期"""
    try:
        start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    except ValueError:
        return {"code": 400, "message": "日期格式错误"}

    # 计算每日产能
    if proc_time_minutes <= 0:
        return {"code": 400, "message": "工序时间必须大于0"}
    daily_cap = (work_hours * 60.0) / proc_time_minutes
    if daily_cap <= 0:
        return {"code": 400, "message": "每日产能计算错误"}

    # 计算所需工作日天数
    days_needed = quantity / daily_cap

    # 计算结束日期（跳过周日和法定节假日）
    end_dt = _add_working_days(start_dt, days_needed)

    return {
        "code": 0,
        "data": {
            "start_date": start_date,
            "end_date": end_dt.strftime("%Y-%m-%d"),
            "days_needed": round(days_needed, 2),
            "working_days": days_needed,
            "daily_cap": round(daily_cap, 1),
            "quantity": quantity,
            "proc_time_minutes": proc_time_minutes,
            "work_hours": work_hours,
        }
    }


@router.get("/machines", summary="获取机床列表")
async def get_machines(db: AsyncSession = Depends(get_db)):
    q_ms_1 = select(ScheduleAssignment.machine_name).where(ScheduleAssignment.machine_name != "")
    q_ms_2 = select(ProductionLog.machine_name).where(ProductionLog.machine_name != "")
    all_ms_stmt = union(q_ms_1, q_ms_2)
    all_ms = (await db.execute(select(all_ms_stmt.subquery().c.machine_name))).scalars().all()
    machines = sorted(set(m for m in all_ms if m))
    return {"code": 0, "data": {"list": machines}}


@router.get("/processes-by-drawing", summary="根据图号获取工序列表")
async def get_processes_by_drawing(drawing_no: str = Query(...), db: AsyncSession = Depends(get_db)):
    normalized_drawing = (drawing_no or "").strip()
    if not normalized_drawing:
        return {"code": 0, "data": []}
    # 使用与 orders-by-drawing 相同的大小写不敏感匹配
    normalized_key = (normalized_drawing or "").lower().replace(" ", "")
    result = await db.execute(
        select(Product).where(
            func.replace(func.lower(func.trim(Product.drawing_no)), " ", "") == normalized_key
        )
    )
    prod = result.scalar_one_or_none()
    if not prod:
        return {"code": 0, "data": []}
    processes = []
    for i in range(1, 9):
        proc_time = getattr(prod, f"proc{i}_time", None)
        if proc_time is not None and float(proc_time) > 0:
            processes.append({"process_name": f"工序{i}", "proc_time_minutes": float(proc_time)})
    return {"code": 0, "data": processes}


@router.post("/assign", summary="创建排产分配")
async def create_assignment(data: dict, db: AsyncSession = Depends(get_db)):
    from models import ScheduleAssignment
    start_date = None
    if data.get("start_date"):
        date_str = data["start_date"]
        time_str = data.get("start_time", "08:00")
        try:
            start_date = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
        except Exception:
            try:
                start_date = datetime.strptime(date_str, "%Y-%m-%d")
            except Exception:
                start_date = None

    assignment = ScheduleAssignment(
        drawing_no=data.get("drawing_no") or "",
        po_no=data.get("po_no") or "",
        seq_no=data.get("seq_no") or "",
        process_name=_normalize_proc(data.get("process_name") or ""),
        machine_name=data.get("machine_name") or "",
        order_quantity=int(data.get("order_quantity") or 0),
        assigned_quantity=int(data.get("assigned_quantity") or 0),
        proc_time_minutes=float(data.get("proc_time_minutes") or 0),
        work_hours=float(data.get("work_hours") or 15.0),
        start_date=start_date,
    )
    db.add(assignment)
    await db.commit()
    await db.refresh(assignment)
    return {"code": 0, "msg": "创建成功", "data": {"id": assignment.id}}


@router.put("/assign/{assignment_id}", summary="更新排产分配")
async def update_assignment(assignment_id: int, data: dict, db: AsyncSession = Depends(get_db)):
    from models import ScheduleAssignment
    result = await db.execute(select(ScheduleAssignment).where(ScheduleAssignment.id == assignment_id))
    assignment = result.scalar_one_or_none()
    if not assignment:
        return {"code": 404, "message": "记录不存在"}

    start_date = assignment.start_date
    if data.get("start_date"):
        date_str = data["start_date"]
        time_str = data.get("start_time", "08:00")
        try:
            start_date = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
        except Exception:
            try:
                start_date = datetime.strptime(date_str, "%Y-%m-%d")
            except Exception:
                start_date = None

    assignment.drawing_no = data.get("drawing_no") or assignment.drawing_no
    assignment.po_no = data.get("po_no") or assignment.po_no
    assignment.seq_no = data.get("seq_no") or assignment.seq_no
    assignment.process_name = _normalize_proc(data.get("process_name") or "") or assignment.process_name
    assignment.machine_name = data.get("machine_name") or assignment.machine_name
    assignment.order_quantity = int(data.get("order_quantity") or assignment.order_quantity)
    assignment.assigned_quantity = int(data.get("assigned_quantity") or assignment.assigned_quantity)
    assignment.proc_time_minutes = float(data.get("proc_time_minutes") or assignment.proc_time_minutes)
    assignment.work_hours = float(data.get("work_hours") or assignment.work_hours)
    assignment.start_date = start_date

    await db.commit()
    return {"code": 0, "msg": "更新成功"}


@router.delete("/assign/by-key", summary="按复合条件删除排产分配")
async def delete_assignment_by_key(
    drawing_no: str = Query(...),
    process_name: str = Query(...),
    machine_name: str = Query(...),
    db: AsyncSession = Depends(get_db)
):
    from models import ScheduleAssignment
    import logging
    logger = logging.getLogger("uvicorn")

    # Use the raw process_name directly - database stores "工序1" not "1"
    proc = process_name  # NO normalization

    # Try both normalized and raw process_name for ScheduleAssignment
    for pn in set([proc, _normalize_proc(proc) if proc != _normalize_proc(proc) else proc]):
        sa_result = await db.execute(
            select(ScheduleAssignment).where(
                ScheduleAssignment.drawing_no == drawing_no,
                ScheduleAssignment.process_name == pn,
                ScheduleAssignment.machine_name == machine_name
            )
        )
        for a in sa_result.scalars().all():
            await db.delete(a)
            logger.warning(f"[DELETE] Deleted SA id={a.id} proc={a.process_name!r}")

    await db.commit()
    return {"code": 0, "msg": "删除成功"}


@router.delete("/assign/{assignment_id}", summary="删除排产分配")
async def delete_assignment(assignment_id: int, db: AsyncSession = Depends(get_db)):
    from models import ScheduleAssignment
    result = await db.execute(select(ScheduleAssignment).where(ScheduleAssignment.id == assignment_id))
    assignment = result.scalar_one_or_none()
    if not assignment:
        return {"code": 404, "message": "记录不存在"}
    await db.delete(assignment)
    await db.commit()
    return {"code": 0, "msg": "删除成功"}
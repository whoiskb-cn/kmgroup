# -*- coding: utf-8 -*-
from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File
from fastapi.responses import StreamingResponse
import pandas as pd
import io
import urllib.parse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import func, or_, select
from pydantic import BaseModel
from typing import Optional
from database import get_db
from import_utils import read_upload_table
from models import InventoryItem, Product
from product_service import ensure_products_by_drawing

router = APIRouter(prefix="/inventory", tags=["仓库库存"])

class InventoryUpdate(BaseModel):
    quantity: Optional[int] = None
    pending_plating: Optional[int] = None
    safety_stock: Optional[int] = None
    warehouse: Optional[str] = None

class InventoryCreate(BaseModel):
    drawing_no: str
    quantity: int = 0
    pending_plating: int = 0


@router.get("/", summary="获取库存列表")
async def list_inventory(
    q: str = Query("", description="搜索图号或代码"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(200, ge=1, le=1000, description="每页条数"),
    db: AsyncSession = Depends(get_db),
):
    # 核心：左外连接库存和产品。如果库存表没这个产品，产品列表里有的也可以搜索到，但这里我们先以库存主表驱动。
    # 为了体验更好：“输入图号就能读取形状”，如果在产品档案里有，但是库存里没有记录，最好的办法是在此接口中自动为产品创建零库存记录。
    
    # 先查询符合条件的产品
    prod_query = select(Product)
    if q:
        prod_query = prod_query.where(
            or_(Product.drawing_no.ilike(f"%{q}%"), Product.code.ilike(f"%{q}%"))
        )
    count_query = select(func.count()).select_from(Product)
    if q:
        count_query = count_query.where(
            or_(Product.drawing_no.ilike(f"%{q}%"), Product.code.ilike(f"%{q}%"))
        )
    total = int((await db.execute(count_query)).scalar() or 0)

    prod_query = (
        prod_query.order_by(Product.updated_at.desc(), Product.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    prod_result = await db.execute(prod_query)
    products = prod_result.scalars().all()
    
    product_ids = [prod.id for prod in products]
    inventory_dict = {}
    if product_ids:
        inv_result = await db.execute(select(InventoryItem).where(InventoryItem.product_id.in_(product_ids)))
        inventory_dict = {item.product_id: item for item in inv_result.scalars().all()}
    
    response_list = []
    
    # 根据符合条件的产品组装数据
    for prod in products:
        inv_item = inventory_dict.get(prod.id)
        response_list.append({
            "id": inv_item.id if inv_item else None,
            "product_id": prod.id,
            "drawing_no": prod.drawing_no or prod.code,
            "material_spec": prod.material_spec,
            "category": prod.category,
            "model_file": prod.model_file,
            "quantity": int(inv_item.quantity or 0) if inv_item else 0,
            "pending_plating": int(inv_item.pending_plating or 0) if inv_item else 0,
            "warehouse": inv_item.warehouse if inv_item else "default",
            "safety_stock": int(inv_item.safety_stock or 10) if inv_item else 10,
            "updated_at": inv_item.updated_at if inv_item else prod.updated_at
        })
        
    # 按更新时间倒序
    response_list.sort(key=lambda x: str(x.get('updated_at', '')), reverse=True)
    
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

@router.put("/{inv_id}", summary="更新产品库存数量")
async def update_inventory(inv_id: int, data: InventoryUpdate, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(InventoryItem).where(InventoryItem.id == inv_id))
    inv = result.scalar_one_or_none()
    if not inv:
        raise HTTPException(status_code=404, detail="库存记录不存在")
        
    if data.quantity is not None:
        inv.quantity = data.quantity
    if data.pending_plating is not None:
        inv.pending_plating = data.pending_plating
    if data.safety_stock is not None:
        inv.safety_stock = data.safety_stock
    if data.warehouse is not None:
        inv.warehouse = data.warehouse
        
    await db.commit()
    await db.refresh(inv)
    return {"code": 0, "message": "库存更新成功"}

@router.post("/", summary="新增库存（按图号自动创建产品）")
async def create_inventory(data: InventoryCreate, db: AsyncSession = Depends(get_db)):
    if not data.drawing_no.strip():
        raise HTTPException(status_code=400, detail="产品图号不能为空")
    
    # 查找产品是否存在
    prod_query = select(Product).where(Product.drawing_no == data.drawing_no)
    result = await db.execute(prod_query)
    prod = result.scalar_one_or_none()
    
    if not prod:
        # 自动创建一条基本的产品档案以便关联，并默认填充代号
        prod = Product(drawing_no=data.drawing_no, code=data.drawing_no)
        db.add(prod)
        await db.flush() # 获取插入的 ID
        
    # 查询是否存在库存
    inv_query = select(InventoryItem).where(InventoryItem.product_id == prod.id)
    inv_res = await db.execute(inv_query)
    inv = inv_res.scalar_one_or_none()
    
    if inv:
        # 已存在直接累加或者覆盖？ 这里理解为建立初始或增加数量比较合理，或者直接当作更新
        inv.quantity += data.quantity
        inv.pending_plating += data.pending_plating
    else:
        # 不存在则新建
        inv = InventoryItem(
            product_id=prod.id,
            quantity=data.quantity,
            pending_plating=data.pending_plating
        )
        db.add(inv)
        
    await db.commit()
    return {"code": 0, "message": "库存新增成功"}

@router.delete("/{inv_id}", summary="删除库存记录")
async def delete_inventory(inv_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(InventoryItem).where(InventoryItem.id == inv_id))
    inv = result.scalar_one_or_none()
    if inv:
        await db.delete(inv)
        await db.commit()
    return {"code": 0, "message": "删除成功"}

@router.post("/batch-import", summary="批量导入库存")
async def import_inventory(file: UploadFile = File(...), db: AsyncSession = Depends(get_db)):
    try:
        df = await read_upload_table(file)

        if "产品图号" not in df.columns:
            raise HTTPException(status_code=400, detail="文件必须包含 '产品图号' 列")
            
        drawing_nos = {
            str(row.get("产品图号", "")).strip()
            for _, row in df.iterrows()
            if str(row.get("产品图号", "")).strip().lower() not in {"", "none", "nan"}
        }
        product_map = await ensure_products_by_drawing(db, drawing_nos)

        product_ids = [prod.id for prod in product_map.values() if getattr(prod, "id", None)]
        inventory_map = {}
        if product_ids:
            inv_res = await db.execute(select(InventoryItem).where(InventoryItem.product_id.in_(product_ids)))
            inventory_map = {inv.product_id: inv for inv in inv_res.scalars().all()}

        count = 0
        for _, row in df.iterrows():
            d_no = str(row.get("产品图号", "")).strip()
            if not d_no or d_no.lower() in ["none", "nan", ""]:
                continue

            try:
                qty = int(row.get("可出货数量", 0) or 0)
                pending = int(row.get("待电镀数量", 0) or 0)
                safety = int(row.get("安全库存", 10) or 10)
            except Exception:
                qty, pending, safety = 0, 0, 10

            wh = row.get("仓库")
            wh = str(wh).strip() if wh is not None else "default"
            if not wh or wh.lower() in ["none", "nan"]:
                wh = "default"

            prod = product_map.get(d_no)
            if not prod:
                continue

            inv = inventory_map.get(prod.id)
            if inv:
                inv.quantity = qty
                inv.pending_plating = pending
                inv.safety_stock = safety
                inv.warehouse = wh
            else:
                inv = InventoryItem(
                    product_id=prod.id,
                    quantity=qty,
                    pending_plating=pending,
                    safety_stock=safety,
                    warehouse=wh
                )
                db.add(inv)
                inventory_map[prod.id] = inv

            count += 1
            
        await db.commit()
        return {"code": 0, "msg": f"成功导入 {count} 条库存记录"}
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"导入失败: {str(e)}")

@router.get("/template", summary="下载库存导入模板")
async def get_template():
    columns = ["产品图号", "可出货数量", "待电镀数量", "安全库存", "仓库"]
    example_data = [["1M15E53603", 500, 200, 100, "1号仓"]]
    df = pd.DataFrame(example_data, columns=columns)
    
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='库存导入模板')
    output.seek(0)
    
    filename = "仓库库存批量添加模板.xlsx"
    encoded_filename = urllib.parse.quote(filename)
    headers = {
        'Content-Disposition': f'attachment; filename="{encoded_filename}"; filename*=UTF-8\'\'{encoded_filename}'
    }
    return StreamingResponse(output, headers=headers, media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')

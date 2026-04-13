# -*- coding: utf-8 -*-
from datetime import datetime
from typing import Optional
import io
import os
import re
import shutil
import urllib.parse

import pandas as pd
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.import_utils import read_upload_table
from app.models import Product


router = APIRouter(prefix="/products", tags=["产品管理"])


class ProductSchema(BaseModel):
    id: int
    code: Optional[str] = None
    name: Optional[str] = None
    category: Optional[str] = None
    unit: str
    drawing_no: Optional[str] = None
    material_spec: Optional[str] = None
    model_file: Optional[str] = None
    can_produce_2_5m: Optional[str] = None
    standard_batch: Optional[str] = None
    proc1_time: Optional[float] = None
    proc2_time: Optional[float] = None
    proc3_time: Optional[float] = None
    proc4_time: Optional[float] = None
    proc5_time: Optional[float] = None
    proc6_time: Optional[float] = None
    proc7_time: Optional[float] = None
    proc8_time: Optional[float] = None
    description: Optional[str] = None
    is_active: bool
    created_at: datetime

    class Config:
        from_attributes = True


class ProductCreate(BaseModel):
    code: Optional[str] = None
    name: Optional[str] = None
    category: Optional[str] = None
    unit: str = "pcs"
    drawing_no: Optional[str] = None
    material_spec: Optional[str] = None
    can_produce_2_5m: Optional[str] = None
    standard_batch: Optional[str] = None
    proc1_time: Optional[float] = None
    proc2_time: Optional[float] = None
    proc3_time: Optional[float] = None
    proc4_time: Optional[float] = None
    proc5_time: Optional[float] = None
    proc6_time: Optional[float] = None
    proc7_time: Optional[float] = None
    proc8_time: Optional[float] = None
    description: Optional[str] = None


class ProductUpdate(BaseModel):
    name: Optional[str] = None
    category: Optional[str] = None
    unit: Optional[str] = None
    drawing_no: Optional[str] = None
    material_spec: Optional[str] = None
    can_produce_2_5m: Optional[str] = None
    standard_batch: Optional[str] = None
    proc1_time: Optional[float] = None
    proc2_time: Optional[float] = None
    proc3_time: Optional[float] = None
    proc4_time: Optional[float] = None
    proc5_time: Optional[float] = None
    proc6_time: Optional[float] = None
    proc7_time: Optional[float] = None
    proc8_time: Optional[float] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None


def _as_float_or_none(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    try:
        return float(value)
    except Exception:
        return 0.0


def _as_clean_str_or_none(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip()
    if text.lower() in ("", "none", "nan"):
        return None
    return text


@router.post("/{id}/upload-model", summary="上传 3D 模型文件")
async def upload_model(id: int, file: UploadFile = File(...), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Product).where(Product.id == id))
    product = result.scalar_one_or_none()
    if not product:
        raise HTTPException(status_code=404, detail="产品档案不存在")

    allowed_exts = (".stp", ".step", ".glb", ".gltf", ".stl")
    original_name = (file.filename or "").strip()
    if not original_name:
        raise HTTPException(status_code=400, detail="文件名不能为空")

    ext = os.path.splitext(original_name.lower())[1]
    if ext not in allowed_exts:
        raise HTTPException(status_code=400, detail="支持格式: STP, STEP, GLB, GLTF, STL")

    upload_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static", "models")
    os.makedirs(upload_dir, exist_ok=True)

    raw_base = f"{product.drawing_no or product.code}_{id}"
    safe_base = re.sub(r"[^A-Za-z0-9._-]+", "_", raw_base).strip("._") or f"product_{id}"
    safe_filename = f"{safe_base}{ext}"
    file_path = os.path.join(upload_dir, safe_filename)

    try:
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        target_model_path = f"/static/models/{safe_filename}"
        response_type = ext[1:]

        # STP/STEP 必须先转 GLB，前端才能实时预览
        if ext in (".stp", ".step"):
            glb_filename = f"{safe_base}.glb"
            glb_path = os.path.join(upload_dir, glb_filename)

            try:
                import cascadio
            except ModuleNotFoundError:
                raise HTTPException(status_code=500, detail="STP 自动转换组件未安装（缺少 cascadio）")

            try:
                cascadio.step_to_glb(file_path, glb_path)
            except Exception as conv_err:
                raise HTTPException(status_code=500, detail=f"STP 转 GLB 失败: {conv_err}")

            if not os.path.exists(glb_path) or os.path.getsize(glb_path) == 0:
                raise HTTPException(status_code=500, detail="STP 转 GLB 失败：未生成有效的 GLB 文件")

            target_model_path = f"/static/models/{glb_filename}"
            response_type = "glb"

        product.model_file = target_model_path
        await db.commit()
        return {
            "code": 0,
            "msg": "模型上传并处理成功" if ext in (".stp", ".step") else "模型上传成功",
            "data": {"path": product.model_file, "type": response_type},
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"模型文件处理失败: {str(e)}")


@router.get("/", summary="获取产品列表")
async def list_products(
    q: Optional[str] = Query(None, description="搜索关键字（代码、名称、图号、客户）"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(200, ge=1, le=1000, description="每页条数"),
    db: AsyncSession = Depends(get_db),
):
    filters = []
    if q:
        filters.append(
            or_(
                Product.code.ilike(f"%{q}%"),
                Product.name.ilike(f"%{q}%"),
                Product.drawing_no.ilike(f"%{q}%"),
                Product.category.ilike(f"%{q}%"),
            )
        )

    count_stmt = select(func.count()).select_from(Product)
    if filters:
        count_stmt = count_stmt.where(*filters)
    total = int((await db.execute(count_stmt)).scalar() or 0)

    stmt = select(Product).order_by(desc(Product.id)).offset((page - 1) * page_size).limit(page_size)
    if filters:
        stmt = stmt.where(*filters)

    result = await db.execute(stmt)
    products = result.scalars().all()
    return {
        "code": 0,
        "data": {
            "list": [ProductSchema.from_orm(p) for p in products],
            "pagination": {
                "page": page,
                "page_size": page_size,
                "total": total,
                "total_pages": (total + page_size - 1) // page_size if total else 0,
            },
        },
    }


@router.post("/", summary="创建新产品")
async def create_product(data: ProductCreate, db: AsyncSession = Depends(get_db)):
    if not data.code:
        data.code = data.drawing_no or f"PN-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    if not data.name:
        data.name = data.drawing_no or "未命名产品"

    existing = await db.execute(select(Product).where(Product.code == data.code))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="产品代码（或同名图号）已存在")

    new_product = Product(**data.dict())
    db.add(new_product)
    await db.commit()
    await db.refresh(new_product)
    return {"code": 0, "msg": "产品新建成功", "data": ProductSchema.from_orm(new_product)}


@router.put("/{id}", summary="更新产品信息")
async def update_product(id: int, data: ProductUpdate, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Product).where(Product.id == id))
    product = result.scalar_one_or_none()
    if not product:
        raise HTTPException(status_code=404, detail="产品不存在")

    update_data = data.dict(exclude_unset=True)
    for key, value in update_data.items():
        setattr(product, key, value)

    await db.commit()
    return {"code": 0, "msg": "更新成功"}


@router.delete("/{id}", summary="删除产品")
async def delete_product(id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Product).where(Product.id == id))
    product = result.scalar_one_or_none()
    if not product:
        raise HTTPException(status_code=404, detail="产品不存在")

    await db.delete(product)
    await db.commit()
    return {"code": 0, "msg": "产品档案已彻底移除"}


@router.post("/batch-import", summary="批量导入产品资料")
async def import_products(file: UploadFile = File(...), db: AsyncSession = Depends(get_db)):
    try:
        df = await read_upload_table(file)

        column_mapping = {
            "产品图号": "drawing_no",
            "产品名称": "name",
            "所属客户": "category",
            "材料规格": "material_spec",
            "2.5M可生产": "can_produce_2_5m",
            "标准批量": "standard_batch",
            "工序1工时": "proc1_time",
            "工序2工时": "proc2_time",
            "工序3工时": "proc3_time",
            "工序4工时": "proc4_time",
            "工序5工时": "proc5_time",
            "工序6工时": "proc6_time",
            "工序7工时": "proc7_time",
            "工序8工时": "proc8_time",
            "备注": "description",
        }

        if "产品图号" not in df.columns:
            raise HTTPException(status_code=400, detail="文件必须包含 '产品图号' 列")

        drawing_nos = {
            drawing_no
            for _, row in df.iterrows()
            for drawing_no in [_as_clean_str_or_none(row.get("产品图号"))]
            if drawing_no
        }
        existing_products = {}
        if drawing_nos:
            existing_res = await db.execute(select(Product).where(Product.drawing_no.in_(drawing_nos)))
            existing_products = {
                product.drawing_no: product for product in existing_res.scalars().all() if product.drawing_no
            }

        imported_count = 0
        updated_count = 0

        for _, row in df.iterrows():
            drawing_no = _as_clean_str_or_none(row.get("产品图号"))
            if not drawing_no:
                continue

            product = existing_products.get(drawing_no)

            attrs = {}
            for cn, fn in column_mapping.items():
                if cn not in row:
                    continue
                value = row[cn]
                if fn.startswith("proc"):
                    attrs[fn] = _as_float_or_none(value)
                else:
                    attrs[fn] = _as_clean_str_or_none(value)

            attrs["drawing_no"] = drawing_no

            if product:
                for k, v in attrs.items():
                    setattr(product, k, v)
                if not product.code:
                    product.code = drawing_no
                if not product.name:
                    product.name = drawing_no
                updated_count += 1
            else:
                if not attrs.get("code"):
                    attrs["code"] = drawing_no
                if not attrs.get("name"):
                    attrs["name"] = drawing_no
                if not attrs.get("unit"):
                    attrs["unit"] = "pcs"
                new_product = Product(**attrs)
                db.add(new_product)
                existing_products[drawing_no] = new_product
                imported_count += 1

        await db.commit()
        return {"code": 0, "msg": f"导入成功: 新增 {imported_count} 条, 更新 {updated_count} 条"}
    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"导入失败: {str(e)}")


@router.get("/template", summary="获取批量导入模板")
async def get_template():
    columns = [
        "产品图号",
        "产品名称",
        "所属客户",
        "材料规格",
        "2.5M可生产",
        "标准批量",
        "工序1工时",
        "工序2工时",
        "工序3工时",
        "工序4工时",
        "工序5工时",
        "工序6工时",
        "工序7工时",
        "工序8工时",
        "备注",
    ]

    example_data = [
        [
            "1M15E53603",
            "固定销轴",
            "客户A",
            "SUS304 φ15",
            "可以",
            "2200",
            1.5,
            2.0,
            None,
            None,
            None,
            None,
            None,
            None,
            "示例产品描述",
        ]
    ]

    df = pd.DataFrame(example_data, columns=columns)

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="产品导入模板")

    output.seek(0)

    filename = "产品管理批量添加模板.xlsx"
    encoded_filename = urllib.parse.quote(filename)
    headers = {
        "Content-Disposition": f"attachment; filename=\"{encoded_filename}\"; filename*=UTF-8''{encoded_filename}"
    }
    return StreamingResponse(
        output,
        headers=headers,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

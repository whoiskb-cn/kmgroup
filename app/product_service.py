# -*- coding: utf-8 -*-
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Product


async def ensure_products_by_drawing(
    db: AsyncSession,
    drawing_nos: Iterable[str],
) -> dict[str, Product]:
    normalized = sorted({str(drawing_no).strip() for drawing_no in drawing_nos if str(drawing_no).strip()})
    if not normalized:
        return {}

    result = await db.execute(select(Product).where(Product.drawing_no.in_(normalized)))
    products = {product.drawing_no: product for product in result.scalars().all() if product.drawing_no}

    missing = [drawing_no for drawing_no in normalized if drawing_no not in products]
    if missing:
        created = [Product(drawing_no=drawing_no, code=drawing_no, name=drawing_no) for drawing_no in missing]
        db.add_all(created)
        await db.flush()
        products.update({product.drawing_no: product for product in created if product.drawing_no})

    return products

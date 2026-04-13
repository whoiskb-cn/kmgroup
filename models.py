# -*- coding: utf-8 -*-
"""
KMGroup 生产管理系统 - ORM 模型
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), comment="创建时间")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=func.now(),
        onupdate=func.now(),
        comment="更新时间",
    )


class User(Base, TimestampMixin):
    __tablename__ = "user"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(50), unique=True, index=True, comment="用户名")
    password_hash: Mapped[str] = mapped_column(String(255), comment="密码哈希")
    role: Mapped[str] = mapped_column(String(20), default="operator", index=True, comment="角色")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, comment="是否激活")


class Product(Base, TimestampMixin):
    __tablename__ = "product"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[Optional[str]] = mapped_column(String(50), unique=True, index=True, comment="产品代码")
    name: Mapped[Optional[str]] = mapped_column(String(100), index=True, comment="产品名称")
    category: Mapped[Optional[str]] = mapped_column(String(50), index=True, comment="所属客户")
    unit: Mapped[str] = mapped_column(String(10), default="pcs", comment="单位")

    drawing_no: Mapped[Optional[str]] = mapped_column(String(100), index=True, comment="产品图号")
    material_spec: Mapped[Optional[str]] = mapped_column(String(200), comment="材料规格")
    model_file: Mapped[Optional[str]] = mapped_column(String(255), comment="模型文件路径")
    can_produce_2_5m: Mapped[Optional[str]] = mapped_column(String(50), comment="2.5 米可生产能力")
    standard_batch: Mapped[Optional[str]] = mapped_column(String(50), comment="标准批量")

    proc1_time: Mapped[Optional[float]] = mapped_column(Numeric(10, 2), nullable=True, comment="工序1工时")
    proc2_time: Mapped[Optional[float]] = mapped_column(Numeric(10, 2), nullable=True, comment="工序2工时")
    proc3_time: Mapped[Optional[float]] = mapped_column(Numeric(10, 2), nullable=True, comment="工序3工时")
    proc4_time: Mapped[Optional[float]] = mapped_column(Numeric(10, 2), nullable=True, comment="工序4工时")
    proc5_time: Mapped[Optional[float]] = mapped_column(Numeric(10, 2), nullable=True, comment="工序5工时")
    proc6_time: Mapped[Optional[float]] = mapped_column(Numeric(10, 2), nullable=True, comment="工序6工时")
    proc7_time: Mapped[Optional[float]] = mapped_column(Numeric(10, 2), nullable=True, comment="工序7工时")
    proc8_time: Mapped[Optional[float]] = mapped_column(Numeric(10, 2), nullable=True, comment="工序8工时")

    description: Mapped[Optional[str]] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class Order(Base, TimestampMixin):
    __tablename__ = "order"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_no: Mapped[str] = mapped_column(String(50), unique=True, index=True, comment="内部订单号")
    po_no: Mapped[Optional[str]] = mapped_column(String(50), index=True, comment="客户 PO 号")
    seq_no: Mapped[Optional[str]] = mapped_column(String(50), index=True, comment="序号")
    product_id: Mapped[Optional[int]] = mapped_column(ForeignKey("product.id"), index=True, comment="关联产品")
    order_quantity: Mapped[int] = mapped_column(Integer, default=0, comment="下单数量")
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True, comment="状态")
    remark: Mapped[Optional[str]] = mapped_column(Text)


class ProductionTask(Base, TimestampMixin):
    __tablename__ = "production_task"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("order.id"), index=True)
    title: Mapped[str] = mapped_column(String(100), comment="任务标题")
    status: Mapped[str] = mapped_column(String(20), default="todo", index=True)
    progress_pct: Mapped[int] = mapped_column(Integer, default=0)
    assigned_to: Mapped[Optional[str]] = mapped_column(String(50), index=True)
    remark: Mapped[Optional[str]] = mapped_column(Text)


class ProductionLog(Base, TimestampMixin):
    __tablename__ = "production_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    report_date: Mapped[datetime] = mapped_column(DateTime, default=func.now(), index=True, comment="报表日期")
    machine_name: Mapped[str] = mapped_column(String(100), index=True, comment="机床名称")
    drawing_no: Mapped[str] = mapped_column(String(100), index=True, comment="产品图号")
    po_no: Mapped[Optional[str]] = mapped_column(String(50), index=True, comment="PO 号")
    seq_no: Mapped[Optional[str]] = mapped_column(String(50), index=True, comment="序号")
    process_name: Mapped[str] = mapped_column(String(100), index=True, comment="工序名称")
    quantity: Mapped[int] = mapped_column(Integer, comment="生产数量")
    processing_time: Mapped[Optional[str]] = mapped_column(String(50), comment="加工时间")
    product_id: Mapped[Optional[int]] = mapped_column(ForeignKey("product.id"), index=True, comment="关联产品")

    product = relationship("Product")


class DailyReport(Base, TimestampMixin):
    __tablename__ = "daily_report"

    id: Mapped[int] = mapped_column(primary_key=True)
    report_date: Mapped[datetime] = mapped_column(DateTime, unique=True, index=True, comment="报表日期")
    summary: Mapped[dict] = mapped_column(JSON, comment="汇总数据")
    note: Mapped[Optional[str]] = mapped_column(Text)


class Shipment(Base, TimestampMixin):
    __tablename__ = "shipment"

    id: Mapped[int] = mapped_column(primary_key=True)
    shipment_date: Mapped[datetime] = mapped_column(DateTime, default=func.now(), index=True, comment="出货日期")
    po_no: Mapped[Optional[str]] = mapped_column(String(50), index=True, comment="PO 号")
    seq_no: Mapped[Optional[str]] = mapped_column(String(50), index=True, comment="序号")
    product_id: Mapped[int] = mapped_column(ForeignKey("product.id"), index=True, comment="关联产品")
    quantity: Mapped[int] = mapped_column(Integer, comment="出货数量")
    customer: Mapped[Optional[str]] = mapped_column(String(100), index=True, comment="所属客户")


class InventoryItem(Base, TimestampMixin):
    __tablename__ = "inventory"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("product.id"), unique=True, index=True)
    quantity: Mapped[int] = mapped_column(Integer, default=0, comment="可出货数量")
    pending_plating: Mapped[int] = mapped_column(Integer, default=0, comment="待电镀数量")
    warehouse: Mapped[str] = mapped_column(String(50), default="default", index=True)
    safety_stock: Mapped[int] = mapped_column(Integer, default=10)


class ProductionProcessState(Base, TimestampMixin):
    __tablename__ = "production_process_state"
    __table_args__ = (
        UniqueConstraint("drawing_no", "po_no", "seq_no", "process_name", name="uq_process_state_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    drawing_no: Mapped[str] = mapped_column(String(100), default="", index=True)
    po_no: Mapped[str] = mapped_column(String(50), default="", index=True)
    seq_no: Mapped[str] = mapped_column(String(50), default="", index=True)
    process_name: Mapped[str] = mapped_column(String(100), default="", index=True)
    is_completed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class ProductionOrderState(Base, TimestampMixin):
    __tablename__ = "production_order_state"
    __table_args__ = (
        UniqueConstraint("drawing_no", "po_no", "seq_no", name="uq_order_state_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    drawing_no: Mapped[str] = mapped_column(String(100), default="", index=True)
    po_no: Mapped[str] = mapped_column(String(50), default="", index=True)
    seq_no: Mapped[str] = mapped_column(String(50), default="", index=True)
    is_completed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

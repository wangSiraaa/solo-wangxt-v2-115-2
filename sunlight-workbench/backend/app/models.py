"""ORM 模型：场景（含坐标基准）、建筑、测点、快照、分析运行与结果。

坐标基准统一在 Scene 上：经纬度锚点(PostGIS geography)、IANA 时区、
模型朝北偏角。任何测点/建筑坐标都是该基准下的模型局部米制坐标。
"""
from __future__ import annotations

from datetime import datetime, date

from geoalchemy2 import Geography, Geometry
from sqlalchemy import (Column, Integer, String, Float, Date, DateTime,
                        ForeignKey, Text, JSON)
from sqlalchemy.orm import relationship

from .db import Base


class Scene(Base):
    __tablename__ = "scenes"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    description = Column(Text, default="")
    latitude = Column(Float, nullable=False)
    longitude = Column(Float, nullable=False)
    timezone = Column(String, nullable=False)          # IANA, 如 Asia/Shanghai
    north_offset_deg = Column(Float, nullable=False, default=0.0)
    anchor = Column(Geography("POINT", srid=4326))     # 模型原点的经纬度
    unmodeled_occluders = Column(Text, default="")     # 树木等未建模遮挡说明
    created_at = Column(DateTime, default=datetime.utcnow)

    buildings = relationship("Building", back_populates="scene",
                             cascade="all, delete-orphan")
    points = relationship("MeasurePoint", back_populates="scene",
                          cascade="all, delete-orphan")
    vegetation = relationship("Vegetation", back_populates="scene",
                              cascade="all, delete-orphan")


class Building(Base):
    __tablename__ = "buildings"
    id = Column(Integer, primary_key=True)
    scene_id = Column(ForeignKey("scenes.id", ondelete="CASCADE"), index=True)
    name = Column(String, nullable=False)
    kind = Column(String, default="building")
    footprint = Column(Geometry("POLYGON", srid=0))    # 模型局部坐标(米)
    base_height = Column(Float, default=0.0)
    top_height = Column(Float, nullable=False)
    color = Column(String, default="#9db2c8")
    scene = relationship("Scene", back_populates="buildings")


class MeasurePoint(Base):
    __tablename__ = "measure_points"
    id = Column(Integer, primary_key=True)
    scene_id = Column(ForeignKey("scenes.id", ondelete="CASCADE"), index=True)
    building_id = Column(ForeignKey("buildings.id"), nullable=True)
    name = Column(String, nullable=False)
    geom = Column(Geometry("POINTZ", srid=0))          # 窗面测点(米)
    normal = Column(JSON, nullable=False)              # 窗面外法线 [x,y,z]
    window_id = Column(String, default="")             # 同一窗面可布多个测点
    scene = relationship("Scene", back_populates="points")


class Vegetation(Base):
    """教学用植被遮挡物：合成几何（树冠体量），按月份参与遮挡。

    与建筑共用"局部坐标多边形 + 高度"的体量表示；active_months 为参与
    遮挡的月份（1–12，叶期）。仅当月启用的植被才进入射线计算。
    """
    __tablename__ = "vegetation"
    id = Column(Integer, primary_key=True)
    scene_id = Column(ForeignKey("scenes.id", ondelete="CASCADE"), index=True)
    name = Column(String, nullable=False)
    footprint = Column(Geometry("POLYGON", srid=0))    # 模型局部坐标(米)
    height = Column(Float, nullable=False)             # 树冠顶高(米)，自地面起算
    active_months = Column(JSON, nullable=False)       # 参与遮挡的月份 [1..12]
    color = Column(String, default="#4caf50")
    scene = relationship("Scene", back_populates="vegetation")


class Snapshot(Base):
    """分析运行时的场景快照：结果永远关联快照而非可变场景。"""
    __tablename__ = "snapshots"
    id = Column(Integer, primary_key=True)
    scene_id = Column(ForeignKey("scenes.id"), index=True)
    payload = Column(JSON, nullable=False)  # 场景+建筑+测点+坐标基准的完整 JSON
    created_at = Column(DateTime, default=datetime.utcnow)


class Run(Base):
    __tablename__ = "runs"
    id = Column(Integer, primary_key=True)
    scene_id = Column(ForeignKey("scenes.id"), index=True)
    snapshot_id = Column(ForeignKey("snapshots.id"), nullable=False)
    run_date = Column(Date, nullable=False)            # 分析的日期
    step_minutes = Column(Integer, nullable=False, default=5)
    params = Column(JSON, default=dict)
    disclaimer = Column(Text, default=(
        "合成场景示例评价口径输出，不构成任何规划合规结论。"))
    created_at = Column(DateTime, default=datetime.utcnow)
    results = relationship("RunPointResult", back_populates="run",
                           cascade="all, delete-orphan")


class RunPointResult(Base):
    __tablename__ = "run_point_results"
    id = Column(Integer, primary_key=True)
    run_id = Column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    point_id = Column(ForeignKey("measure_points.id"))
    hourly_samples = Column(JSON, nullable=False)      # 逐时口径（快览）
    continuous_intervals = Column(JSON, nullable=False)  # 连续口径（时段）
    fine_samples = Column(JSON, nullable=False)        # 细步长逐样本(含遮挡物)
    summary = Column(JSON, nullable=False)
    run = relationship("Run", back_populates="results")

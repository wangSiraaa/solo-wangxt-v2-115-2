"""FastAPI 入口：场景 / 测点 / 教学植被 / 分析运行 / 快照 / 单点遮挡追查。"""
from __future__ import annotations

import math
from datetime import datetime

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from geoalchemy2.shape import to_shape, from_shape
from pydantic import BaseModel, ConfigDict, Field, field_validator
from shapely.geometry import Polygon as ShapelyPolygon
from sqlalchemy.orm import Session

from . import models, seed
from .analysis import MeasurePointGeom, analyze_point
from .db import Base, engine, get_db
from .geometry import (BuildingGeom, OWNER_BUILDING, OWNER_VEGETATION,
                       build_scene)
from .solar import enu_to_model, sun_vector_enu, solar_positions
import pandas as pd

app = FastAPI(title="日照分析工作台（合成场景·示例口径）")
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])

DISCLAIMER = ("合成场景 + 示例评价口径输出，未建模遮挡（树木等）见场景说明；"
              "逐时采样≠连续日照时长；本结果不构成规划合规结论。")


@app.on_event("startup")
def startup():
    Base.metadata.create_all(engine)


# ---------- 场景 ----------

class SceneOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    description: str
    latitude: float
    longitude: float
    timezone: str
    north_offset_deg: float
    unmodeled_occluders: str


def _scene_json(s: models.Scene) -> dict:
    return SceneOut.model_validate(s).model_dump()


@app.get("/api/scenes")
def list_scenes(db: Session = Depends(get_db)):
    return [_scene_json(s) for s in db.query(models.Scene).all()]


@app.post("/api/scenes/seed")
def seed_scenes(db: Session = Depends(get_db)):
    if db.query(models.Scene).count():
        raise HTTPException(409, "已有场景，拒绝重复种子")
    return {"scene_ids": seed.seed_database(db)}


def _load_scene_bundle(db: Session, scene_id: int):
    s = db.get(models.Scene, scene_id)
    if not s:
        raise HTTPException(404, "场景不存在")
    buildings = db.query(models.Building).filter_by(scene_id=scene_id).all()
    vegetation = db.query(models.Vegetation).filter_by(scene_id=scene_id).all()
    points = db.query(models.MeasurePoint).filter_by(scene_id=scene_id).all()
    return s, buildings, vegetation, points


def _veg_row(v: models.Vegetation) -> dict:
    return {
        "id": v.id, "name": v.name, "color": v.color,
        "footprint": list(to_shape(v.footprint).exterior.coords)[:-1],
        "base_height": v.base_height, "crown_height": v.crown_height,
        "leaf_months": list(v.leaf_months),
        "occluder_type": OWNER_VEGETATION,
    }


def _bundle_payload(s, buildings, vegetation, points) -> dict:
    """场景完整 JSON（即快照内容，前端渲染也用它）。

    植被连同 leaf_months 一并冻结进快照：旧运行重放时按运行日期的月份
    决定哪些植被参与射线，后续编辑植被不影响既有快照。
    """
    return {
        "scene": _scene_json(s),
        "buildings": [{
            "id": b.id, "name": b.name, "kind": b.kind, "color": b.color,
            "footprint": list(to_shape(b.footprint).exterior.coords)[:-1],
            "base_height": b.base_height, "top_height": b.top_height,
            "occluder_type": OWNER_BUILDING,
        } for b in buildings],
        "vegetation": [_veg_row(v) for v in vegetation],
        "points": [{
            "id": p.id, "name": p.name, "window_id": p.window_id,
            "position": list(to_shape(p.geom).coords[0]), "normal": p.normal,
        } for p in points],
    }


@app.get("/api/scenes/{scene_id}")
def get_scene(scene_id: int, db: Session = Depends(get_db)):
    return _bundle_payload(*_load_scene_bundle(db, scene_id))


# ---------- 教学用植被遮挡物（合成几何） ----------

def _validate_veg(name, footprint, base_height, crown_height, leaf_months):
    """植被入参校验：非法月份或无效几何一律拒绝保存。"""
    if not isinstance(name, str) or not name.strip():
        raise HTTPException(422, "植被名称不能为空")
    if not isinstance(footprint, list) or len(footprint) < 3:
        raise HTTPException(422, "树冠多边形至少需要 3 个顶点")
    coords = []
    for c in footprint:
        if (not isinstance(c, (list, tuple)) or len(c) != 2
                or not all(isinstance(v, (int, float)) and not isinstance(v, bool)
                           and math.isfinite(v) for v in c)):
            raise HTTPException(422, "多边形顶点必须是有限数值的 [x, y] 对（模型局部米制坐标）")
        coords.append((float(c[0]), float(c[1])))
    if coords[0] == coords[-1]:  # 闭合环重复末点
        coords = coords[:-1]
        if len(coords) < 3:
            raise HTTPException(422, "树冠多边形至少需要 3 个顶点")
    if (not isinstance(base_height, (int, float))
            or not isinstance(crown_height, (int, float))
            or isinstance(base_height, bool) or isinstance(crown_height, bool)
            or not math.isfinite(base_height) or not math.isfinite(crown_height)):
        raise HTTPException(422, "高度必须为有限数值（米）")
    base_height, crown_height = float(base_height), float(crown_height)
    if base_height < 0 or crown_height <= base_height:
        raise HTTPException(422, "高度非法：需 0 ≤ base_height < crown_height")
    if not isinstance(leaf_months, list) or not leaf_months:
        raise HTTPException(422, "leaf_months 必须是非空的 1-12 月份数组")
    months = []
    for m in leaf_months:
        if isinstance(m, bool) or not isinstance(m, int) or not 1 <= m <= 12:
            raise HTTPException(422, "非法月份：leaf_months 只接受 1-12 的整数")
        if m not in months:
            months.append(m)
    months.sort()
    try:
        poly = ShapelyPolygon(coords)
        if not poly.is_valid or poly.area <= 0:
            raise ValueError
    except (ValueError, TypeError):
        raise HTTPException(422, "树冠多边形几何无效（自交/退化/面积为 0），拒绝保存")
    return name.strip(), coords, base_height, crown_height, months


class VegetationIn(BaseModel):
    name: str
    footprint: list[list[float]] = Field(..., min_length=3)  # 树冠投影多边形
    base_height: float = 0.0
    crown_height: float
    leaf_months: list[int] = Field(..., min_length=1)        # 1-12 的有叶月份
    color: str = "#5d8f4e"

    @field_validator("leaf_months", mode="before")
    @classmethod
    def _check_months(cls, v):
        # 必须在 Pydantic 把 true 强转为 1 之前拦截布尔
        if isinstance(v, list):
            for m in v:
                if isinstance(m, bool) or not isinstance(m, int) or not 1 <= m <= 12:
                    raise ValueError("leaf_months 只接受 1-12 的整数月份")
        return v

    @field_validator("base_height", "crown_height")
    @classmethod
    def _finite(cls, v):
        # 不用 ValueError：避免 FastAPI 把 nan/inf 原值回显进 422 响应体
        # （标准库 json 不允许非有限浮点），HTTPException 不回显输入值
        if not math.isfinite(v):
            raise HTTPException(422, "高度必须是有限数值（米）")
        return v


@app.post("/api/scenes/{scene_id}/vegetation")
def add_vegetation(scene_id: int, veg: VegetationIn,
                   db: Session = Depends(get_db)):
    if not db.get(models.Scene, scene_id):
        raise HTTPException(404, "场景不存在")
    name, coords, base_h, crown_h, months = _validate_veg(
        veg.name, veg.footprint, veg.base_height, veg.crown_height,
        veg.leaf_months)
    row = models.Vegetation(
        scene_id=scene_id, name=name,
        footprint=from_shape(ShapelyPolygon(coords), srid=0),
        base_height=base_h, crown_height=crown_h, leaf_months=months,
        color=veg.color)
    db.add(row)
    db.commit()
    db.refresh(row)
    return _veg_row(row)


class VegetationPatch(BaseModel):
    name: str | None = None
    footprint: list[list[float]] | None = None
    base_height: float | None = None
    crown_height: float | None = None
    leaf_months: list[int] | None = None
    color: str | None = None

    @field_validator("leaf_months", mode="before")
    @classmethod
    def _check_months(cls, v):
        if isinstance(v, list):
            for m in v:
                if isinstance(m, bool) or not isinstance(m, int) or not 1 <= m <= 12:
                    raise ValueError("leaf_months 只接受 1-12 的整数月份")
        return v

    @field_validator("base_height", "crown_height")
    @classmethod
    def _finite(cls, v):
        if v is not None and not math.isfinite(v):
            raise HTTPException(422, "高度必须是有限数值（米）")
        return v


@app.put("/api/scenes/{scene_id}/vegetation/{veg_id}")
def update_vegetation(scene_id: int, veg_id: int, patch: VegetationPatch,
                      db: Session = Depends(get_db)):
    row = (db.query(models.Vegetation)
           .filter_by(id=veg_id, scene_id=scene_id).first())
    if not row:
        raise HTTPException(404, "植被不存在")
    current = _veg_row(row)
    name = patch.name if patch.name is not None else current["name"]
    footprint = (patch.footprint if patch.footprint is not None
                 else current["footprint"])
    base_h = (patch.base_height if patch.base_height is not None
              else current["base_height"])
    crown_h = (patch.crown_height if patch.crown_height is not None
               else current["crown_height"])
    months = (patch.leaf_months if patch.leaf_months is not None
              else current["leaf_months"])
    name, coords, base_h, crown_h, months = _validate_veg(
        name, footprint, base_h, crown_h, months)
    row.name, row.footprint = name, from_shape(ShapelyPolygon(coords), srid=0)
    row.base_height, row.crown_height, row.leaf_months = base_h, crown_h, months
    if patch.color is not None:
        row.color = patch.color
    db.commit()
    db.refresh(row)
    return _veg_row(row)


@app.delete("/api/scenes/{scene_id}/vegetation/{veg_id}")
def delete_vegetation(scene_id: int, veg_id: int,
                      db: Session = Depends(get_db)):
    row = (db.query(models.Vegetation)
           .filter_by(id=veg_id, scene_id=scene_id).first())
    if not row:
        raise HTTPException(404, "植被不存在")
    db.delete(row)
    db.commit()
    return {"deleted": veg_id}


# ---------- 分析 ----------

class RunRequest(BaseModel):
    scene_id: int
    date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    step_minutes: int = Field(5, ge=1, le=60)
    point_ids: list[int] | None = None  # 缺省 = 场景全部测点


def _geom_from_bundle(buildings, points, vegetation=None, active_month: int | None = None):
    """从 ORM 行重建几何与测点。

    建筑恒参与；植被只在 active_month ∈ leaf_months 时送入射线
    （active_month=None 表示无日期上下文，全部不启用）。
    """
    geoms = [BuildingGeom(name=b.name, owner_type=OWNER_BUILDING,
                          footprint=[tuple(c) for c in
                                     to_shape(b.footprint).exterior.coords][:-1],
                          base_height=b.base_height, top_height=b.top_height)
             for b in buildings]
    active_veg = [v for v in (vegetation or [])
                  if active_month in list(v.leaf_months)]
    for v in active_veg:
        geoms.append(BuildingGeom(
            name=v.name, owner_type=OWNER_VEGETATION,
            footprint=[tuple(c) for c in to_shape(v.footprint).exterior.coords][:-1],
            base_height=v.base_height, top_height=v.crown_height))
    built = build_scene(geoms)
    pts = [MeasurePointGeom(id=str(p.id), name=p.name,
                            position=tuple(to_shape(p.geom).coords[0]),
                            normal=tuple(p.normal))
           for p in points]
    return built, pts


@app.post("/api/analysis/run")
def run_analysis(req: RunRequest, db: Session = Depends(get_db)):
    s, buildings, vegetation, points = _load_scene_bundle(db, req.scene_id)
    if req.point_ids:
        points = [p for p in points if p.id in req.point_ids]
        if not points:
            raise HTTPException(400, "point_ids 无匹配测点")
    run_month = int(req.date[5:7])
    # 1) 快照先行：完整植被清单（含 leaf_months）冻结，结果关联快照，
    #    场景/植被后续被改动也不影响追溯
    payload = _bundle_payload(s, buildings, vegetation, points)
    snap = models.Snapshot(scene_id=s.id, payload=payload)
    db.add(snap)
    db.flush()
    run = models.Run(scene_id=s.id, snapshot_id=snap.id,
                     run_date=datetime.strptime(req.date, "%Y-%m-%d").date(),
                     step_minutes=req.step_minutes,
                     params={"point_ids": [p.id for p in points],
                             "month": run_month,
                             "active_vegetation": sorted(
                                 v.name for v in vegetation
                                 if run_month in list(v.leaf_months))},
                     disclaimer=DISCLAIMER)
    db.add(run)
    db.flush()
    # 2) 只有当月启用（在 leaf_months 内）的植被才进入射线几何
    built, pts = _geom_from_bundle(buildings, points, vegetation,
                                   active_month=run_month)
    for pt in pts:
        r = analyze_point(built, pt, latitude=s.latitude, longitude=s.longitude,
                          tz=s.timezone, date=req.date,
                          north_offset_deg=s.north_offset_deg,
                          step_minutes=req.step_minutes)
        db.add(models.RunPointResult(
            run_id=run.id, point_id=int(pt.id),
            hourly_samples=r["hourly_samples"],
            continuous_intervals=r["continuous_intervals"],
            fine_samples=r["fine_samples"], summary=r["summary"]))
    db.commit()
    return {"run_id": run.id, "snapshot_id": snap.id, "disclaimer": DISCLAIMER}


@app.get("/api/analysis/{run_id}")
def get_run(run_id: int, db: Session = Depends(get_db)):
    run = db.get(models.Run, run_id)
    if not run:
        raise HTTPException(404, "运行不存在")
    return {
        "run_id": run.id, "scene_id": run.scene_id,
        "snapshot_id": run.snapshot_id, "date": str(run.run_date),
        "step_minutes": run.step_minutes, "disclaimer": run.disclaimer,
        "params": run.params or {},
        "results": [{
            "point_id": r.point_id,
            "summary": r.summary,
            "hourly_samples": r.hourly_samples,
            "continuous_intervals": r.continuous_intervals,
            "fine_samples": r.fine_samples,
        } for r in run.results],
    }


@app.get("/api/analysis/{run_id}/points/{point_id}/trace")
def trace_point(run_id: int, point_id: int, time: str | None = None,
                db: Session = Depends(get_db)):
    """单点追查：返回该点逐样本遮挡物（区分建筑/植被）；给定 time 时只返回该时刻。"""
    r = (db.query(models.RunPointResult)
         .filter_by(run_id=run_id, point_id=point_id).first())
    if not r:
        raise HTTPException(404, "结果不存在")
    samples = r.fine_samples
    if time:
        samples = [s for s in samples if s["time"].startswith(time)]
        if not samples:
            raise HTTPException(404, "该时刻无采样（注意步长与本地时区）")
    shaded = [s for s in samples if s["status"] == "shaded"]
    return {
        "point_id": point_id,
        "queried": len(samples), "shaded": len(shaded),
        "occluders": sorted({s["occluder"] for s in shaded}),
        "occluder_types": {
            s["occluder"]: s.get("occluder_type", "building") for s in shaded
        },
        "samples": samples,
        "note": ("遮挡物名称来自运行时场景快照；植被为教学用合成几何，"
                 "仅当月（leaf_months）启用的植被参与计算；"
                 "场景说明中“未建模遮挡”的文字不是物体，不进入射线。"),
    }


# ---------- 快照 ----------

@app.get("/api/snapshots/{snapshot_id}")
def get_snapshot(snapshot_id: int, db: Session = Depends(get_db)):
    snap = db.get(models.Snapshot, snapshot_id)
    if not snap:
        raise HTTPException(404, "快照不存在")
    return {"snapshot_id": snap.id, "created_at": str(snap.created_at),
            "payload": snap.payload}


# ---------- 太阳路径（前端可视化） ----------

@app.get("/api/scenes/{scene_id}/sunpath")
def sunpath(scene_id: int, date: str, db: Session = Depends(get_db)):
    s = db.get(models.Scene, scene_id)
    if not s:
        raise HTTPException(404, "场景不存在")
    times = pd.date_range(pd.Timestamp(date, tz=s.timezone),
                          periods=96, freq="15min")
    pos = solar_positions(s.latitude, s.longitude, s.timezone, times)
    out = []
    for t, row in zip(times, pos.itertuples()):
        if row.apparent_elevation <= 0:
            continue
        d = enu_to_model(sun_vector_enu(row.apparent_elevation, row.azimuth),
                         s.north_offset_deg)
        out.append({"time": t.isoformat(), "dir": [round(float(v), 5) for v in d],
                    "elevation": round(float(row.apparent_elevation), 2),
                    "azimuth": round(float(row.azimuth), 2)})
    return {"date": date, "timezone": s.timezone, "points": out}

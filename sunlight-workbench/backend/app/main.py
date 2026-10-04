"""FastAPI 入口：场景 / 测点 / 教学用植被 / 分析运行 / 快照 / 单点遮挡追查。"""
from __future__ import annotations

from datetime import datetime

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from geoalchemy2.shape import from_shape, to_shape
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator
from shapely.geometry import Polygon
from sqlalchemy.orm import Session

from . import models, seed
from .analysis import MeasurePointGeom, analyze_point
from .db import Base, engine, get_db
from .geometry import (KIND_BUILDING, KIND_VEGETATION, BuildingGeom,
                       VegetationGeom, build_scene, cast_sun_ray,
                       validate_footprint)
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
    points = db.query(models.MeasurePoint).filter_by(scene_id=scene_id).all()
    vegetation = db.query(models.Vegetation).filter_by(scene_id=scene_id).all()
    return s, buildings, points, vegetation


def _vegetation_json(v) -> dict:
    """植被的 JSON 表示（快照与 API 共用同一结构）。"""
    return {
        "id": v.id, "name": v.name, "color": v.color,
        "footprint": [list(c) for c in
                      list(to_shape(v.footprint).exterior.coords)[:-1]],
        "height": v.height, "active_months": list(v.active_months),
    }


def _bundle_payload(s, buildings, points, vegetation=None) -> dict:
    """场景完整 JSON（即快照内容，前端渲染也用它）。

    植被与建筑分数组保存：运行快照据此区分两类遮挡物；快照存全量植被，
    某次运行实际启用哪些由 run.params.active_vegetation 记录。
    """
    return {
        "scene": _scene_json(s),
        "buildings": [{
            "id": b.id, "name": b.name, "kind": b.kind, "color": b.color,
            "footprint": list(to_shape(b.footprint).exterior.coords)[:-1],
            "base_height": b.base_height, "top_height": b.top_height,
        } for b in buildings],
        "vegetation": [_vegetation_json(v) for v in vegetation or []],
        "points": [{
            "id": p.id, "name": p.name, "window_id": p.window_id,
            "position": list(to_shape(p.geom).coords[0]), "normal": p.normal,
        } for p in points],
    }


@app.get("/api/scenes/{scene_id}")
def get_scene(scene_id: int, db: Session = Depends(get_db)):
    return _bundle_payload(*_load_scene_bundle(db, scene_id))


# ---------- 教学用植被遮挡物 ----------

class VegetationIn(BaseModel):
    """植被保存输入：沿用建筑的多边形体量表示，额外有高度与启用月份。

    非法月份 / 无效几何在此直接拒绝（422），不会写入数据库。
    """
    name: str = Field(..., min_length=1, max_length=60)
    footprint: list[list[float]]
    height: float = Field(..., gt=0, le=200)     # 树冠顶高(米)
    active_months: list[StrictInt]               # 参与遮挡的月份 1–12
    color: str = Field("#4caf50", pattern=r"^#[0-9a-fA-F]{6}$")

    @field_validator("name")
    @classmethod
    def _name_nonempty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("名称不能为空")
        return v

    @field_validator("footprint")
    @classmethod
    def _footprint_valid(cls, v):
        return validate_footprint(v)

    @field_validator("active_months")
    @classmethod
    def _months_valid(cls, v):
        if not v:
            raise ValueError("active_months 不能为空（至少一个启用月份）")
        for m in v:
            if not 1 <= m <= 12:
                raise ValueError(f"非法月份 {m}：必须在 1–12 之间")
        return sorted(set(v))


def _ensure_unique_occluder_name(db: Session, scene_id: int, name: str,
                                 exclude_vegetation_id: int | None = None):
    """遮挡物按名称追查，植被名不得与场景内建筑/其他植被重名。"""
    if db.query(models.Building).filter_by(scene_id=scene_id, name=name).first():
        raise HTTPException(409, "名称与场景内建筑重复，遮挡物追查会产生歧义")
    q = db.query(models.Vegetation).filter_by(scene_id=scene_id, name=name)
    if exclude_vegetation_id is not None:
        q = q.filter(models.Vegetation.id != exclude_vegetation_id)
    if q.first():
        raise HTTPException(409, "名称与场景内其他植被重复")


@app.get("/api/scenes/{scene_id}/vegetation")
def list_vegetation(scene_id: int, db: Session = Depends(get_db)):
    if not db.get(models.Scene, scene_id):
        raise HTTPException(404, "场景不存在")
    return [_vegetation_json(v) for v in
            db.query(models.Vegetation).filter_by(scene_id=scene_id).all()]


@app.post("/api/scenes/{scene_id}/vegetation", status_code=201)
def create_vegetation(scene_id: int, body: VegetationIn,
                      db: Session = Depends(get_db)):
    if not db.get(models.Scene, scene_id):
        raise HTTPException(404, "场景不存在")
    _ensure_unique_occluder_name(db, scene_id, body.name)
    v = models.Vegetation(
        scene_id=scene_id, name=body.name,
        footprint=from_shape(Polygon(body.footprint), srid=0),
        height=body.height, active_months=body.active_months, color=body.color)
    db.add(v)
    db.commit()
    db.refresh(v)
    return _vegetation_json(v)


@app.put("/api/vegetation/{vegetation_id}")
def update_vegetation(vegetation_id: int, body: VegetationIn,
                      db: Session = Depends(get_db)):
    v = db.get(models.Vegetation, vegetation_id)
    if not v:
        raise HTTPException(404, "植被不存在")
    _ensure_unique_occluder_name(db, v.scene_id, body.name,
                                 exclude_vegetation_id=v.id)
    v.name = body.name
    v.footprint = from_shape(Polygon(body.footprint), srid=0)
    v.height = body.height
    v.active_months = body.active_months
    v.color = body.color
    db.commit()
    return _vegetation_json(v)


@app.delete("/api/vegetation/{vegetation_id}")
def delete_vegetation(vegetation_id: int, db: Session = Depends(get_db)):
    v = db.get(models.Vegetation, vegetation_id)
    if not v:
        raise HTTPException(404, "植被不存在")
    db.delete(v)
    db.commit()
    return {"deleted": vegetation_id}


def _active_vegetation(vegetation, date_str: str):
    """只保留在分析日期所在月份启用的植被（叶期才进入射线计算）。"""
    month = datetime.strptime(date_str, "%Y-%m-%d").month
    return [v for v in vegetation if month in (v.active_months or [])]


# ---------- 分析 ----------

class RunRequest(BaseModel):
    scene_id: int
    date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    step_minutes: int = Field(5, ge=1, le=60)
    point_ids: list[int] | None = None  # 缺省 = 场景全部测点


def _geom_from_bundle(buildings, points, vegetation=None):
    geoms = [BuildingGeom(name=b.name,
                          footprint=[tuple(c) for c in
                                     to_shape(b.footprint).exterior.coords][:-1],
                          base_height=b.base_height, top_height=b.top_height)
             for b in buildings]
    veg_geoms = [VegetationGeom(name=v.name,
                                footprint=[tuple(c) for c in
                                           to_shape(v.footprint).exterior.coords][:-1],
                                height=v.height)
                 for v in vegetation or []]
    built = build_scene(geoms, veg_geoms)
    pts = [MeasurePointGeom(id=str(p.id), name=p.name,
                            position=tuple(to_shape(p.geom).coords[0]),
                            normal=tuple(p.normal))
           for p in points]
    return built, pts


@app.post("/api/analysis/run")
def run_analysis(req: RunRequest, db: Session = Depends(get_db)):
    s, buildings, points, vegetation = _load_scene_bundle(db, req.scene_id)
    if req.point_ids:
        points = [p for p in points if p.id in req.point_ids]
        if not points:
            raise HTTPException(400, "point_ids 无匹配测点")
    # 只有当月启用的植被才进入射线计算；快照仍存全量植被
    run_date = datetime.strptime(req.date, "%Y-%m-%d").date()
    active_veg = _active_vegetation(vegetation, req.date)
    # 1) 快照先行：结果关联快照，场景后续被改动也不影响追溯
    payload = _bundle_payload(s, buildings, points, vegetation)
    snap = models.Snapshot(scene_id=s.id, payload=payload)
    db.add(snap)
    db.flush()
    run = models.Run(scene_id=s.id, snapshot_id=snap.id,
                     run_date=run_date,
                     step_minutes=req.step_minutes,
                     params={"point_ids": [p.id for p in points],
                             "month": run_date.month,
                             "active_vegetation": [
                                 {"id": v.id, "name": v.name} for v in active_veg]},
                     disclaimer=DISCLAIMER)
    db.add(run)
    db.flush()
    built, pts = _geom_from_bundle(buildings, points, active_veg)
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
        "params": run.params,  # 含当月启用的植被（month / active_vegetation）
        "results": [{
            "point_id": r.point_id,
            "summary": r.summary,
            "hourly_samples": r.hourly_samples,
            "continuous_intervals": r.continuous_intervals,
            "fine_samples": r.fine_samples,
        } for r in run.results],
    }


def _snapshot_kind_map(payload: dict) -> dict:
    """从快照 payload 建 遮挡物名 → 类别(building/vegetation) 映射。

    以快照为准而非当前场景：植被被编辑/删除后，旧运行的追查仍按原快照归属。
    """
    m = {b["name"]: KIND_BUILDING for b in payload.get("buildings", [])}
    m.update({v["name"]: KIND_VEGETATION for v in payload.get("vegetation", [])})
    return m


@app.get("/api/analysis/{run_id}/points/{point_id}/trace")
def trace_point(run_id: int, point_id: int, time: str | None = None,
                db: Session = Depends(get_db)):
    """单点追查：返回该点逐样本遮挡物（含建筑/植被类别）；给定 time 时只返回该时刻。"""
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
    run = db.get(models.Run, run_id)
    snap = db.get(models.Snapshot, run.snapshot_id) if run else None
    kind_map = _snapshot_kind_map(snap.payload) if snap else {}
    details = [{"name": name,
                # 新运行的样本自带 occluder_kind；旧运行回退到快照名称归属
                "kind": next((s.get("occluder_kind") for s in shaded
                              if s["occluder"] == name
                              and s.get("occluder_kind")),
                             kind_map.get(name, KIND_BUILDING))}
               for name in sorted({s["occluder"] for s in shaded})]
    return {
        "point_id": point_id,
        "queried": len(samples), "shaded": len(shaded),
        "occluders": [d["name"] for d in details],
        "occluder_details": details,
        "samples": samples,
        "note": ("遮挡物名称来自场景快照几何；植被为教学用合成几何，"
                 "仅在启用月份参与遮挡；未建模遮挡（树木等）见场景说明。"),
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

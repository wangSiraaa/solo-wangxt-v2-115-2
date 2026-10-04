"""端到端逻辑测试（SQLite 内存库）：快照不可变 + 月份过滤 + 文字说明不入几何。

PostGIS 不可用时用 WKT 文本列代替 Geometry 列（shapely 解析口径一致），
真实 PostGIS 读写由 docker-compose 集成环境覆盖。
"""
import pytest
from sqlalchemy import Column, Float, ForeignKey, Integer, JSON, String, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from shapely.geometry import Point, Polygon
from shapely import wkt

from app.analysis import MeasurePointGeom, analyze_point
from app.geometry import BuildingGeom, build_scene
from app.main import _validate_veg
from app.seed import scene_specs, LAT, LON, TZ


class TBase(DeclarativeBase):
    pass


class TScene(TBase):
    __tablename__ = "t_scenes"
    id = Column(Integer, primary_key=True)
    name = Column(String)
    description = Column(Text, default="")
    latitude = Column(Float)
    longitude = Column(Float)
    timezone = Column(String)
    north_offset_deg = Column(Float, default=0.0)
    unmodeled_occluders = Column(Text, default="")


class TBuilding(TBase):
    __tablename__ = "t_buildings"
    id = Column(Integer, primary_key=True)
    scene_id = Column(ForeignKey("t_scenes.id"))
    name = Column(String)
    kind = Column(String, default="building")
    footprint = Column(Text)  # WKT 替身
    base_height = Column(Float, default=0.0)
    top_height = Column(Float)
    color = Column(String, default="#9db2c8")


class TVegetation(TBase):
    __tablename__ = "t_vegetation"
    id = Column(Integer, primary_key=True)
    scene_id = Column(ForeignKey("t_scenes.id"))
    name = Column(String)
    footprint = Column(Text)
    base_height = Column(Float, default=0.0)
    crown_height = Column(Float)
    leaf_months = Column(JSON)
    color = Column(String, default="#5d8f4e")


class TPoint(TBase):
    __tablename__ = "t_points"
    id = Column(Integer, primary_key=True)
    scene_id = Column(ForeignKey("t_scenes.id"))
    name = Column(String)
    window_id = Column(String, default="")
    geom = Column(Text)
    normal = Column(JSON)


class TSnapshot(TBase):
    __tablename__ = "t_snapshots"
    id = Column(Integer, primary_key=True)
    scene_id = Column(ForeignKey("t_scenes.id"))
    payload = Column(JSON)


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    TBase.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


def _seed(db, spec):
    sc = TScene(name=spec["name"], description=spec["description"],
                latitude=LAT, longitude=LON, timezone=TZ,
                north_offset_deg=spec["north_offset_deg"],
                unmodeled_occluders="南侧悬铃木未建模（文字说明，不是物体）")
    db.add(sc); db.flush()
    for b in spec["buildings"]:
        db.add(TBuilding(scene_id=sc.id, name=b["name"], kind=b["kind"],
                         footprint=Polygon(b["footprint"]).wkt,
                         base_height=b["base_height"], top_height=b["top_height"],
                         color=b["color"]))
    for v in spec["vegetation"]:
        db.add(TVegetation(scene_id=sc.id, name=v["name"],
                          footprint=Polygon(v["footprint"]).wkt,
                          base_height=v["base_height"], crown_height=v["crown_height"],
                          leaf_months=list(v["leaf_months"]), color=v["color"]))
    for p in spec["points"]:
        db.add(TPoint(scene_id=sc.id, name=p["name"], window_id=p["window_id"],
                      geom=Point(*p["position"]).wkt, normal=list(p["normal"])))
    db.commit()
    return sc


def _bundle(db, sc):
    """与 app.main._bundle_payload 同构（适配 WKT 替身行）。"""
    buildings = db.query(TBuilding).filter_by(scene_id=sc.id).all()
    veg = db.query(TVegetation).filter_by(scene_id=sc.id).all()
    points = db.query(TPoint).filter_by(scene_id=sc.id).all()
    return {
        "scene": {"id": sc.id, "name": sc.name, "latitude": sc.latitude,
                  "longitude": sc.longitude, "timezone": sc.timezone,
                  "north_offset_deg": sc.north_offset_deg,
                  "unmodeled_occluders": sc.unmodeled_occluders},
        "buildings": [{
            "id": b.id, "name": b.name, "kind": b.kind, "color": b.color,
            "footprint": list(wkt.loads(b.footprint).exterior.coords)[:-1],
            "base_height": b.base_height, "top_height": b.top_height,
            "occluder_type": "building"} for b in buildings],
        "vegetation": [{
            "id": v.id, "name": v.name, "color": v.color,
            "footprint": list(wkt.loads(v.footprint).exterior.coords)[:-1],
            "base_height": v.base_height, "crown_height": v.crown_height,
            "leaf_months": list(v.leaf_months),
            "occluder_type": "vegetation"} for v in veg],
        "points": [{"id": p.id, "name": p.name, "window_id": p.window_id,
                    "position": list(wkt.loads(p.geom).coords[0]),
                    "normal": p.normal} for p in points],
    }


def _geoms(payload, month):
    gs = [BuildingGeom(name=b["name"], owner_type="building",
                       footprint=[tuple(c) for c in b["footprint"]],
                       base_height=b["base_height"], top_height=b["top_height"])
          for b in payload["buildings"]]
    gs += [BuildingGeom(name=v["name"], owner_type="vegetation",
                        footprint=[tuple(c) for c in v["footprint"]],
                        base_height=v["base_height"], top_height=v["crown_height"])
           for v in payload["vegetation"] if month in v["leaf_months"]]
    return build_scene(gs)


def _run(payload, date):
    month = int(date[5:7])
    built = _geoms(payload, month)
    out = {}
    for p in payload["points"]:
        pt = MeasurePointGeom(id=str(p["id"]), name=p["name"],
                              position=tuple(p["position"]), normal=tuple(p["normal"]))
        out[p["name"]] = analyze_point(built, pt, latitude=LAT, longitude=LON,
                                       tz=TZ, date=date,
                                       north_offset_deg=payload["scene"]["north_offset_deg"],
                                       step_minutes=30)
    return out


def test_veg_shading_differs_by_month(db):
    """验收核心：W4_三层西窗 1 月(停用)无植被遮挡，7 月(启用)被 V2 遮挡，
    且 7 月启用/停用的日照分钟差异可解释为树冠本身。"""
    sc = _seed(db, scene_specs()[0])
    payload = _bundle(db, sc)

    jan = _run(payload, "2026-01-15")
    jul_on = _run(payload, "2026-07-15")
    # 1 月：没有任何样本的遮挡物类型是 vegetation
    assert all(s.get("occluder_type") != "vegetation"
               for s in jan["W4_三层西窗_中"]["fine_samples"])
    # 7 月：V2 承担明确遮挡量
    v2 = [s for s in jul_on["W4_三层西窗_中"]["fine_samples"]
          if s.get("occluder") == "V2_西侧庭荫树"]
    assert v2 and all(s["occluder_type"] == "vegetation" for s in v2)
    assert all(s["status"] == "shaded" for s in v2)
    # 7 月停用对照：把植被全部拿掉，日照必然更多
    jul_off_payload = {**payload, "vegetation": []}
    jul_off = _run(jul_off_payload, "2026-07-15")
    assert (jul_off["W4_三层西窗_中"]["summary"]["sunlit_minutes"]
            > jul_on["W4_三层西窗_中"]["summary"]["sunlit_minutes"])
    # E1 7 月被 V1 遮挡；1 月无植被命中
    assert any(s.get("occluder") == "V1_东侧行道树"
               for s in jul_on["E1_三层东窗_中"]["fine_samples"])
    assert all(s.get("occluder_type") != "vegetation"
               for s in jan["E1_三层东窗_中"]["fine_samples"])


def test_snapshot_immutable_after_vegetation_edit(db):
    """编辑植被后，旧运行仍按原快照展示。"""
    sc = _seed(db, scene_specs()[0])
    payload_jul = _bundle(db, sc)
    jul = _run(payload_jul, "2026-07-15")
    w4_veg_before = sum(1 for s in jul["W4_三层西窗_中"]["fine_samples"]
                        if s.get("occluder") == "V2_西侧庭荫树")
    db.add(TSnapshot(scene_id=sc.id, payload=payload_jul)); db.commit()
    snap_id = db.query(TSnapshot).one().id

    # 事后编辑 V2：改名、移到西窗近前、改高度、改为全年有叶
    v2 = db.query(TVegetation).filter_by(name="V2_西侧庭荫树").one()
    name, coords, bh, th, months = _validate_veg(
        "V2_改名移位", [[-20, -3], [-17, -3], [-17, 0], [-20, 0]],
        2.0, 18.0, list(range(1, 13)))
    v2.name, v2.footprint = name, Polygon(coords).wkt
    v2.base_height, v2.crown_height, v2.leaf_months = bh, th, months
    db.commit()

    frozen = db.get(TSnapshot, snap_id).payload
    old_v2 = next(v for v in frozen["vegetation"]
                  if v["name"] == "V2_西侧庭荫树")
    assert old_v2["leaf_months"] == [4, 5, 6, 7, 8, 9, 10]
    assert old_v2["crown_height"] == 12.0
    assert all(v["name"] != "V2_改名移位" for v in frozen["vegetation"])
    # 用冻结快照重放 7 月：结果样本数与遮挡物完全照旧
    replay = _run(frozen, "2026-07-15")
    assert sum(1 for s in replay["W4_三层西窗_中"]["fine_samples"]
               if s.get("occluder") == "V2_西侧庭荫树") == w4_veg_before

    # 实时场景重算 1 月：新树全年有叶，确实进入射线
    live = _bundle(db, sc)
    jan = _run(live, "2026-01-15")
    assert any(s.get("occluder") == "V2_改名移位"
               for s in jan["W4_三层西窗_中"]["fine_samples"])

def test_invalid_vegetation_cannot_be_saved():
    good = [[0, 0], [4, 0], [4, 4], [0, 4]]
    for bad in [
        dict(name="", fp=good, bh=2, th=9, m=[6]),
        dict(name="V", fp=[[0, 0], [1, 0]], bh=2, th=9, m=[6]),
        dict(name="V", fp=good, bh=2, th=9, m=[]),
        dict(name="V", fp=good, bh=2, th=9, m=[0]),
        dict(name="V", fp=good, bh=2, th=9, m=[13]),
        dict(name="V", fp=good, bh=2, th=9, m=["7"]),
        dict(name="V", fp=good, bh=2, th=9, m=[True]),
        dict(name="V", fp=good, bh=9, th=9, m=[7]),
        dict(name="V", fp=[[0, 0], [1, 0], [2, 0]], bh=2, th=9, m=[7]),
        dict(name="V", fp=[[0, 0], [4, 0], [0, 0], [4, 4]], bh=2, th=9, m=[7]),
    ]:
        with pytest.raises(Exception):
            _validate_veg(bad["name"], bad["fp"], bad["bh"], bad["th"], bad["m"])
    # 合法输入可以保存
    out = _validate_veg("V", good, 2, 9, [7, 7, 8])
    assert out[4] == [7, 8]  # 去重+排序


def test_unmodeled_text_never_becomes_geometry(db):
    sc = _seed(db, scene_specs()[0])
    payload = _bundle(db, sc)
    names = [x["name"] for x in payload["buildings"] + payload["vegetation"]]
    assert not any("悬铃木" in n or "杆塔" in n for n in names)
    assert "悬铃木" in payload["scene"]["unmodeled_occluders"]
    assert payload["scene"]["unmodeled_occluders"]  # 免责说明照常返回

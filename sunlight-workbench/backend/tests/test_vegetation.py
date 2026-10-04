"""教学用植被遮挡物测试。

核心算例可解释性：同一测点、同一日期，唯一变量是"植被是否当月启用"
（启用列表由 _active_vegetation 按月份过滤产生，与 run_analysis 同一口径）：
- 启用：夏季清晨低角度太阳被东侧树冠遮挡，occluder_kind=vegetation
- 停用：同一时刻晒到，树冠不进入射线计算
另覆盖：非法月份/无效几何拒绝保存、快照结构、遮挡物类别归属、旋转不变性。
"""
from types import SimpleNamespace as NS

import numpy as np
import pytest
from geoalchemy2.shape import from_shape
from pydantic import ValidationError
from shapely.geometry import Point, Polygon

from app.analysis import MeasurePointGeom, analyze_point
from app.geometry import (BuildingGeom, VegetationGeom, box_footprint,
                          build_scene, cast_sun_ray, rotate_point)
from app.main import (VegetationIn, _active_vegetation, _bundle_payload,
                      _geom_from_bundle, _snapshot_kind_map)
from app.seed import LAT, LON, TZ, scene_specs
from app.solar import enu_to_model, sun_vector_enu

# 测点：目标楼一层南窗右点（与种子场景 W1_一层南窗_右 一致）
POINT = MeasurePointGeom(id="P1", name="南窗右点",
                         position=(8.0, -7.0, 1.6), normal=(0.0, -1.0, 0.0))
# 教学用树冠：东南侧 6×6×10 m，叶期 4–10 月（与种子场景 V1 一致）
TREE = VegetationGeom(name="V1_东南乔木",
                      footprint=box_footprint(14.0, -12.0, 6.0, 6.0),
                      height=10.0)
# 远处配楼：不参与本算例遮挡，仅保证无植被场景几何非空
FAR = BuildingGeom(name="far_north",
                   footprint=box_footprint(0.0, 60.0, 10.0, 10.0),
                   base_height=0.0, top_height=5.0)

VALID = dict(name="V1", footprint=[[0, 0], [4, 0], [4, 4], [0, 4]],
             height=8.0, active_months=[4, 5, 6, 7, 8, 9, 10])


# ---------- 输入校验：非法月份 / 无效几何不能保存 ----------

def test_valid_vegetation_accepted():
    v = VegetationIn(**VALID)
    assert v.active_months == [4, 5, 6, 7, 8, 9, 10]
    # 重复与乱序月份归一化为有序去重列表
    v2 = VegetationIn(**{**VALID, "active_months": [10, 4, 7, 4]})
    assert v2.active_months == [4, 7, 10]


@pytest.mark.parametrize("months", [
    [],              # 空：至少一个启用月份
    [0], [13], [-3], # 超出 1–12
    [5, 12, 13],     # 含一个非法即整体拒绝
    ["x"], [7.5],    # 非整数
    [True],          # 布尔不是月份
])
def test_invalid_months_rejected(months):
    with pytest.raises(ValidationError):
        VegetationIn(**{**VALID, "active_months": months})


@pytest.mark.parametrize("fp", [
    [[0, 0], [1, 1]],                          # 少于 3 个顶点
    [[0, 0], [4, 4], [4, 0], [0, 4]],          # 自相交（蝴蝶结）
    [[0, 0], [1, 1], [2, 2]],                  # 共线，面积为 0
    [[0, 0, 0], [4, 0], [4, 4]],               # 顶点维度错误
    [[0, 0], [4, 0], [float("nan"), 4], [0, 4]],  # 非有限坐标
    "not-a-polygon",
])
def test_invalid_geometry_rejected(fp):
    with pytest.raises(ValidationError):
        VegetationIn(**{**VALID, "footprint": fp})


@pytest.mark.parametrize("h", [0, -3.5, float("inf"), float("nan"), 500.0])
def test_invalid_height_rejected(h):
    with pytest.raises(ValidationError):
        VegetationIn(**{**VALID, "height": h})


def test_http_layer_rejects_invalid_without_db():
    """HTTP 层：非法月份/无效几何 → 422，请求体校验在触库前完成（不能保存）。"""
    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app, raise_server_exceptions=False)
    for bad in [{**VALID, "active_months": [13]},
                {**VALID, "active_months": []},
                {**VALID, "footprint": [[0, 0], [4, 4], [4, 0], [0, 4]]},
                {**VALID, "footprint": [[0, 0], [1, 1]]},
                {**VALID, "height": 0}]:
        r = client.post("/api/scenes/1/vegetation", json=bad)
        assert r.status_code == 422, bad


# ---------- 月份过滤 ----------

def test_active_vegetation_month_filter():
    rows = [NS(name="V_叶期", active_months=[4, 5, 6, 7, 8, 9, 10]),
            NS(name="V_常绿", active_months=list(range(1, 13)))]
    assert [v.name for v in _active_vegetation(rows, "2026-07-15")] == \
        ["V_叶期", "V_常绿"]
    assert [v.name for v in _active_vegetation(rows, "2026-01-15")] == ["V_常绿"]
    assert _active_vegetation([], "2026-07-15") == []


# ---------- 射线计算区分建筑与植被 ----------

def test_ray_distinguishes_building_and_vegetation():
    origin = np.array(POINT.position) + np.array(POINT.normal) * 0.01
    # 东南偏南 40° 太阳：手算命中树冠北立面（y=-9，z≈6.5 < 10）
    d = enu_to_model(sun_vector_enu(40.0, 110.0), 0.0)
    hit = cast_sun_ray(build_scene([FAR], [TREE]), origin, d)
    assert hit["occluder"] == "V1_东南乔木"
    assert hit["occluder_kind"] == "vegetation"
    assert hit["hit_point"][1] == pytest.approx(-9.0, abs=1e-3)
    # 同一射线，植被不启用 → 无遮挡
    assert cast_sun_ray(build_scene([FAR]), origin, d) is None
    # 建筑遮挡仍标记为 building
    wall = BuildingGeom(name="wall_south",
                        footprint=box_footprint(0.0, -11.5, 10.0, 3.0),
                        base_height=0.0, top_height=4.0)
    d_south = enu_to_model(sun_vector_enu(10.0, 180.0), 0.0)
    hit_b = cast_sun_ray(build_scene([wall]),
                         np.array([0.0, 0.0, 1.2]) + np.array([0.0, -1.0, 0.0]) * 0.01,
                         d_south)
    assert hit_b["occluder_kind"] == "building"


def test_same_point_active_vs_inactive_month():
    """验收算例：同一测点同一日期（2026-07-15），唯一差异是植被当月是否
    启用——启用时清晨被树冠遮挡，停用时同一时段晒到。"""
    date = "2026-07-15"
    veg_row = NS(name=TREE.name, active_months=[4, 5, 6, 7, 8, 9, 10])
    assert _active_vegetation([veg_row], date) == [veg_row]      # 7 月启用
    assert _active_vegetation([veg_row], "2026-01-15") == []     # 1 月停用
    kw = dict(latitude=39.9, longitude=116.4, tz="Asia/Shanghai",
              north_offset_deg=0.0, step_minutes=5)
    on = analyze_point(build_scene([FAR], [TREE]), POINT, date=date, **kw)
    off = analyze_point(build_scene([FAR]), POINT, date=date, **kw)
    diff = [(a, b) for a, b in zip(on["fine_samples"], off["fine_samples"])
            if a["status"] != b["status"]]
    assert diff, "植被启用应产生可观测的遮挡差异"
    for a, b in diff:
        # 每个差异样本都可解释：启用=被树冠遮挡，停用=晒到
        assert a["status"] == "shaded" and a["occluder"] == TREE.name
        assert a["occluder_kind"] == "vegetation"
        assert b["status"] == "sunlit"
        # 差异只出现在上午（太阳位于东南低角度时，约 09:50–10:55）
        assert 9 <= int(a["time"][11:13]) <= 11
    assert on["summary"]["sunlit_minutes"] < off["summary"]["sunlit_minutes"]
    # 停用场景全天无任何植被遮挡物
    assert all(s.get("occluder_kind") != "vegetation"
               for s in off["fine_samples"])


# ---------- 种子场景端到端（经 _geom_from_bundle 重建几何） ----------

def _fake_building_rows(spec):
    return [NS(id=i, name=b["name"], kind=b["kind"], color=b["color"],
               footprint=from_shape(Polygon(b["footprint"]), srid=0),
               base_height=b["base_height"], top_height=b["top_height"])
            for i, b in enumerate(spec["buildings"], 1)]


def _fake_vegetation_rows(spec):
    return [NS(id=i, name=v["name"],
               footprint=from_shape(Polygon(v["footprint"]), srid=0),
               height=v["height"], active_months=v["active_months"],
               color=v["color"])
            for i, v in enumerate(spec["vegetation"], 1)]


def test_seed_scene_july_vs_january():
    """种子场景 S1：7 月（叶期）东南乔木遮挡南窗右点上午低角度太阳；
    1 月（落叶）植被不进入射线计算，全天遮挡物均为建筑。"""
    spec = scene_specs()[0]
    buildings = _fake_building_rows(spec)
    veg = _fake_vegetation_rows(spec)
    kw = dict(latitude=LAT, longitude=LON, tz=TZ,
              north_offset_deg=0.0, step_minutes=5)
    built_jul, _ = _geom_from_bundle(
        buildings, [], _active_vegetation(veg, "2026-07-15"))
    jul = analyze_point(built_jul, POINT, date="2026-07-15", **kw)
    veg_shaded = [s for s in jul["fine_samples"]
                  if s["status"] == "shaded"
                  and s.get("occluder_kind") == "vegetation"]
    assert veg_shaded, "7 月上午应出现树冠遮挡"
    assert {s["occluder"] for s in veg_shaded} == {"V1_东南乔木"}
    # 连续时段中的植被区间带类别标记
    veg_iv = [iv for iv in jul["continuous_intervals"]
              if iv.get("occluder_kind") == "vegetation"]
    assert veg_iv and all(iv["occluder"] == "V1_东南乔木" for iv in veg_iv)

    built_jan, _ = _geom_from_bundle(
        buildings, [], _active_vegetation(veg, "2026-01-15"))
    jan = analyze_point(built_jan, POINT, date="2026-01-15", **kw)
    shaded_jan = [s for s in jan["fine_samples"] if s["status"] == "shaded"]
    assert shaded_jan, "1 月仍应被邻楼遮挡（原建筑分析不受影响）"
    assert all(s["occluder_kind"] == "building" for s in shaded_jan)


def test_rotation_invariance_with_vegetation():
    """含植被时旋转口径仍成立：S1 与 S2（几何转 30° + north_offset=30°）
    同日期逐样本判定一致（含植被遮挡样本）。"""
    s1, s2 = scene_specs()

    def build(spec, date):
        geoms = [BuildingGeom(name=b["name"], footprint=b["footprint"],
                              base_height=b["base_height"],
                              top_height=b["top_height"])
                 for b in spec["buildings"]]
        veg = [VegetationGeom(name=v.name, footprint=v.footprint,
                              height=v.height)
               for v in _active_vegetation(
                   [NS(**v) for v in spec["vegetation"]], date)]
        return build_scene(geoms, veg)

    date = "2026-07-15"
    px, py = rotate_point(POINT.position[:2], 30.0)
    nx, ny = rotate_point(POINT.normal[:2], 30.0)
    point2 = MeasurePointGeom(id="P1", name="南窗中点",
                              position=(px, py, 1.6), normal=(nx, ny, 0.0))
    kw = dict(latitude=LAT, longitude=LON, tz=TZ, date=date, step_minutes=10)
    r1 = analyze_point(build(s1, date), POINT, north_offset_deg=0.0, **kw)
    r2 = analyze_point(build(s2, date), point2, north_offset_deg=30.0, **kw)
    assert any(s.get("occluder_kind") == "vegetation"
               for s in r1["fine_samples"]), "算例应包含植被遮挡样本"
    for a, b in zip(r1["fine_samples"], r2["fine_samples"]):
        assert a["status"] == b["status"], a["time"]
        assert a["occluder"] == b["occluder"]
        assert a.get("occluder_kind") == b.get("occluder_kind")


# ---------- 快照结构与遮挡物类别归属 ----------

def test_snapshot_payload_contains_vegetation():
    spec = scene_specs()[0]
    scene = NS(id=1, name=spec["name"], description=spec["description"],
               latitude=LAT, longitude=LON, timezone=TZ,
               north_offset_deg=0.0, unmodeled_occluders="树木未建模")
    buildings = _fake_building_rows(spec)
    veg = _fake_vegetation_rows(spec)
    points = [NS(id=1, name="W1", window_id="W1",
                 geom=from_shape(Point(0.0, -7.0, 1.6), srid=0),
                 normal=[0.0, -1.0, 0.0])]
    payload = _bundle_payload(scene, buildings, points, veg)
    # 植被与建筑分数组保存，名称/高度/启用月份齐全
    assert len(payload["buildings"]) == 3
    assert [v["name"] for v in payload["vegetation"]] == \
        ["V1_东南乔木", "V2_西南乔木"]
    v0 = payload["vegetation"][0]
    assert v0["height"] == 10.0
    assert v0["active_months"] == [4, 5, 6, 7, 8, 9, 10]
    assert len(v0["footprint"]) == 4
    # 不传植被时快照结构仍完整（向后兼容旧调用）
    assert _bundle_payload(scene, buildings, points)["vegetation"] == []


def test_snapshot_kind_map_distinguishes():
    payload = {"buildings": [{"name": "B1"}, {"name": "T_目标楼"}],
               "vegetation": [{"name": "V1_东南乔木"}]}
    assert _snapshot_kind_map(payload) == {
        "B1": "building", "T_目标楼": "building", "V1_东南乔木": "vegetation"}
    # 旧快照（无 vegetation 键）也能正常归属
    assert _snapshot_kind_map({"buildings": [{"name": "B1"}]}) == {
        "B1": "building"}


def test_snapshot_payload_decoupled_from_later_edits():
    """快照 payload 是独立拷贝：生成后修改植被行（如启用月份），
    已生成的快照内容不变——旧运行仍按原快照展示。"""
    spec = scene_specs()[0]
    scene = NS(id=1, name=spec["name"], description=spec["description"],
               latitude=LAT, longitude=LON, timezone=TZ,
               north_offset_deg=0.0, unmodeled_occluders="树木未建模")
    veg = _fake_vegetation_rows(spec)
    payload = _bundle_payload(scene, _fake_building_rows(spec), [], veg)
    assert payload["vegetation"][0]["active_months"] == [4, 5, 6, 7, 8, 9, 10]
    # 模拟"运行后编辑植被"：直接改动行对象
    veg[0].active_months.append(12)
    veg[0].height = 30.0
    assert payload["vegetation"][0]["active_months"] == [4, 5, 6, 7, 8, 9, 10]
    assert payload["vegetation"][0]["height"] == 10.0

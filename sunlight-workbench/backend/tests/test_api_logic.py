"""API 层逻辑测试（不依赖 PostgreSQL，用内存对象模拟 ORM 行）。

真实 PostGIS 读写由 docker-compose 集成环境覆盖；这里验证：
- 快照 payload 结构（结果-快照关联的数据源，含教学植被+leaf_months）
- 从 ORM 行重建几何 + 测点并跑通分析
- 按运行月份过滤植被（只有当月启用的植被进入射线）
- 场景坐标基准字段齐全（经纬度/时区/朝北偏角三元组）
"""
from types import SimpleNamespace as NS

import pytest
from geoalchemy2.shape import from_shape
from shapely.geometry import Point, Polygon

from app.main import (
    _bundle_payload, _geom_from_bundle, _validate_veg, VegetationIn)
from app.analysis import analyze_point
from app.seed import scene_specs, LAT, LON, TZ


def _fake_rows():
    spec = scene_specs()[0]
    scene = NS(id=1, name=spec["name"], description=spec["description"],
               latitude=LAT, longitude=LON, timezone=TZ,
               north_offset_deg=spec["north_offset_deg"],
               unmodeled_occluders="树木未建模")
    buildings = [NS(id=i, name=b["name"], kind=b["kind"], color=b["color"],
                    footprint=from_shape(Polygon(b["footprint"]), srid=0),
                    base_height=b["base_height"], top_height=b["top_height"])
                 for i, b in enumerate(spec["buildings"], 1)]
    vegetation = [NS(id=i, name=v["name"], color=v["color"],
                     footprint=from_shape(Polygon(v["footprint"]), srid=0),
                     base_height=v["base_height"], crown_height=v["crown_height"],
                     leaf_months=list(v["leaf_months"]))
                  for i, v in enumerate(spec["vegetation"], 1)]
    points = [NS(id=i, name=p["name"], window_id=p["window_id"],
                 geom=from_shape(Point(*p["position"]), srid=0),
                 normal=list(p["normal"]))
              for i, p in enumerate(spec["points"], 1)]
    return scene, buildings, vegetation, points


def test_snapshot_payload_structure():
    scene, buildings, vegetation, points = _fake_rows()
    payload = _bundle_payload(scene, buildings, vegetation, points)
    sc = payload["scene"]
    # 坐标基准三元组必须在快照里完整保存
    assert (sc["latitude"], sc["longitude"]) == (LAT, LON)
    assert sc["timezone"] == TZ
    assert sc["north_offset_deg"] == 0.0
    assert len(payload["buildings"]) == 3
    assert len(payload["vegetation"]) == 2
    assert len(payload["points"]) == 7
    p0 = payload["points"][0]
    assert len(p0["position"]) == 3 and len(p0["normal"]) == 3
    # 植被的几何、高度与有叶月份全部冻结进快照
    v1 = next(v for v in payload["vegetation"] if v["name"] == "V1_东侧行道树")
    assert len(v1["footprint"]) == 4
    assert v1["base_height"] < v1["crown_height"]
    assert v1["leaf_months"] == [4, 5, 6, 7, 8, 9, 10]
    assert v1["occluder_type"] == "vegetation"
    # “未建模遮挡”的说明文字只在 scene 上，不会变成几何物体
    assert sc["unmodeled_occluders"]
    assert all(b["occluder_type"] == "building" for b in payload["buildings"])


def test_geom_rebuild_and_analyze():
    scene, buildings, vegetation, points = _fake_rows()
    built, pts = _geom_from_bundle(buildings, points[:1], vegetation,
                                   active_month=7)
    r = analyze_point(built, pts[0], latitude=scene.latitude,
                      longitude=scene.longitude, tz=scene.timezone,
                      date="2026-07-15",
                      north_offset_deg=scene.north_offset_deg, step_minutes=30)
    assert r["summary"]["sunlit_minutes"] >= 0
    shaded = [s for s in r["fine_samples"] if s["status"] == "shaded"]
    assert all(s["occluder"] in {"B1_南侧板楼", "B2_东南塔楼", "T_目标楼",
                                 "V1_东侧行道树", "V2_西侧庭荫树"}
               for s in shaded)
    assert all(s["occluder_type"] in {"building", "vegetation"} for s in shaded)


def test_vegetation_month_filtering():
    """同一测点：1 月植被停用 vs 7 月启用，结果差异必须可解释。

    W4_三层西窗 基线 1 月日照 140min；7 月有叶时 V2 截掉下午约 140min。
    """
    scene, buildings, vegetation, points = _fake_rows()
    w4 = next(p for p in points if p.name == "W4_三层西窗_中")

    def result(month, date):
        built, pts = _geom_from_bundle(buildings, [w4], vegetation,
                                      active_month=month)
        return analyze_point(built, pts[0], latitude=LAT, longitude=LON,
                             tz=TZ, date=date, north_offset_deg=0.0,
                             step_minutes=15)

    jan_off = result(1, "2026-01-15")
    jul_on = result(7, "2026-07-15")
    # 1 月停用：没有任何植被遮挡物出现
    assert not any(s.get("occluder_type") == "vegetation"
                   for s in jan_off["fine_samples"])
    # 7 月启用：V2_西侧庭荫树 必须实际承担遮挡，且类型标记为 vegetation
    veg_shaded = [s for s in jul_on["fine_samples"]
                  if s.get("occluder") == "V2_西侧庭荫树"]
    assert veg_shaded and all(
        s["occluder_type"] == "vegetation" and s["status"] == "shaded"
        for s in veg_shaded)
    # 同一场景同一日期，把植被拿掉后这些样本应为晒到（差异来自植被本身）
    built_no_veg, pts = _geom_from_bundle(buildings, [w4], [],
                                          active_month=7)
    jul_off = analyze_point(built_no_veg, pts[0], latitude=LAT, longitude=LON,
                            tz=TZ, date="2026-07-15",
                            north_offset_deg=0.0, step_minutes=15)
    assert (jul_off["summary"]["sunlit_minutes"]
            > jul_on["summary"]["sunlit_minutes"])
    # 3 月（落叶月，不在 leaf_months）与无植被几何完全一致
    built_mar, _ = _geom_from_bundle(buildings, [w4], vegetation,
                                     active_month=3)
    mar = analyze_point(built_mar, pts[0], latitude=LAT, longitude=LON,
                        tz=TZ, date="2026-03-20", north_offset_deg=0.0,
                        step_minutes=15)
    mar_no_veg = analyze_point(built_no_veg, pts[0], latitude=LAT,
                               longitude=LON, tz=TZ, date="2026-03-20",
                               north_offset_deg=0.0, step_minutes=15)
    assert ([(s["status"], s["occluder"]) for s in mar["fine_samples"]]
            == [(s["status"], s["occluder"]) for s in mar_no_veg["fine_samples"]])


def test_validate_veg_rejects_bad_inputs():
    """非法月份或无效几何不能保存（422）。"""
    good_fp = [[0, 0], [4, 0], [4, 4], [0, 4]]
    # 基线：合法输入应通过
    name, coords, bh, th, months = _validate_veg(
        "V_test", good_fp, 2.0, 9.0, [6, 7, 8])
    assert months == [6, 7, 8] and name == "V_test"

    bad_cases = [
        ("", good_fp, 2, 9, [6]),                    # 空名称
        ("V", [[0, 0], [4, 0]], 2, 9, [6]),          # 顶点不足
        ("V", [[0, 0], [4, 0], [4, 4], [0, 4]],
         2, 9, []),                                   # 空月份
        ("V", good_fp, 2, 9, [0]),                   # 非法月份 0
        ("V", good_fp, 2, 9, [13]),                  # 非法月份 13
        ("V", good_fp, 2, 9, ["7"]),                 # 月份非整数
        ("V", good_fp, 2, 9, [7.0]),                 # 月份非整数
        ("V", good_fp, 2, 9, [True]),                # bool 不算月份
        ("V", good_fp, 9, 9, [7]),                   # 高度倒挂
        ("V", good_fp, -1, 9, [7]),                  # 负枝下高
        ("V", good_fp, float("nan"), 9, [7]),        # NaN 高度
        ("V", good_fp, 2, float("inf"), [7]),        # inf 冠顶
        ("V", [[0, 0], [float("nan"), 0], [4, 4]],
         2, 9, [7]),                                  # 非有限坐标
        ("V", [[0, 0], [1, 0], [2, 0]], 2, 9, [7]),  # 三点共线退化（面积 0）
        ("V", [[0, 0], [4, 0], [0, 0], [4, 4]],
         2, 9, [7]),                                  # 自交蝴蝶结多边形
        ("V", [["a", 0], [4, 0], [4, 4]], 2, 9, [7]),# 非数值坐标
    ]
    for args in bad_cases:
        with pytest.raises(Exception) as ei:
            _validate_veg(*args)
        # 几何上抛 ValueError 或 HTTPException(422) 都属于“拒绝保存”
        assert ei.value.__class__.__name__ in {"HTTPException", "ValueError"}
        if ei.value.__class__.__name__ == "HTTPException":
            assert ei.value.status_code == 422


def test_pydantic_model_basic_constraints():
    v = VegetationIn(name="V", footprint=[[0, 0], [1, 0], [1, 1]],
                     crown_height=8, leaf_months=[5, 6])
    assert v.base_height == 0.0
    with pytest.raises(Exception):
        VegetationIn(name="V", footprint=[[0, 0]], crown_height=8,
                     leaf_months=[5])

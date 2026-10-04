"""几何建模与射线相交（trimesh）。

- 建筑以拉伸多边形表示：footprint(模型坐标, 米) + base_height + top_height。
- 教学用植被沿用同一多边形体量表示（footprint + height，自地面起算），
  仅以 kind="vegetation" 区分；是否参与遮挡由分析时按月份过滤决定，与几何无关。
- 所有网格合并为一个 trimesh.Trimesh 做射线求交，face_owner 记录每个面
  属于哪个遮挡物，face_kind 记录其类别（building/vegetation），从而支持
  "单点追查遮挡物"并在结果中区分建筑与植被。
- 射线原点沿窗面法向外偏移 RAY_ORIGIN_OFFSET，避免与自身墙面自相交。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import trimesh
from shapely.geometry import Polygon

RAY_ORIGIN_OFFSET = 0.01  # 米，沿窗面外法线

KIND_BUILDING = "building"
KIND_VEGETATION = "vegetation"


@dataclass
class BuildingGeom:
    name: str
    footprint: list[tuple[float, float]]  # 模型坐标多边形
    base_height: float = 0.0
    top_height: float = 10.0
    kind: str = "building"  # building / podium / ground_obstacle


@dataclass
class VegetationGeom:
    """教学用植被遮挡物：与建筑相同的多边形体量，自地面拉伸到 height。"""
    name: str
    footprint: list[tuple[float, float]]  # 模型坐标多边形
    height: float = 8.0                   # 树冠顶高(米)
    kind: str = KIND_VEGETATION


@dataclass
class BuiltScene:
    mesh: trimesh.Trimesh
    face_owner: np.ndarray  # 每个三角面 -> 遮挡物名
    face_kind: np.ndarray   # 每个三角面 -> KIND_BUILDING / KIND_VEGETATION
    names: list[str] = field(default_factory=list)


def extrude_footprint(footprint: list[tuple[float, float]],
                      base_height: float, top_height: float) -> trimesh.Trimesh:
    poly = Polygon(footprint)
    if not poly.is_valid:
        poly = poly.buffer(0)
    mesh = trimesh.creation.extrude_polygon(poly, height=top_height - base_height)
    mesh.apply_translation([0, 0, base_height])
    return mesh


def box_footprint(cx: float, cy: float, w: float, d: float) -> list[tuple[float, float]]:
    """以 (cx,cy) 为中心的矩形 footprint（合成场景算例用）。"""
    return [(cx - w / 2, cy - d / 2), (cx + w / 2, cy - d / 2),
            (cx + w / 2, cy + d / 2), (cx - w / 2, cy + d / 2)]


def validate_footprint(coords) -> list[list[float]]:
    """校验多边形 footprint（植被保存入口用）：≥3 顶点、坐标为有限数值、
    多边形有效（不自交）且面积 > 0。

    非法几何直接抛 ValueError 拒绝保存，不做 buffer(0) 静默修复——
    教学场景要求"保存的就是输入的"，避免修复后几何与预期不符。
    """
    if not isinstance(coords, (list, tuple)) or len(coords) < 3:
        raise ValueError("footprint 至少需要 3 个顶点")
    pts = []
    for c in coords:
        if (not isinstance(c, (list, tuple)) or len(c) != 2
                or not all(math.isfinite(float(v)) for v in c)):
            raise ValueError("footprint 顶点必须为 [x, y] 有限数值")
        pts.append([float(c[0]), float(c[1])])
    poly = Polygon(pts)
    if not poly.is_valid or poly.area <= 1e-9:
        raise ValueError("footprint 多边形无效（自相交或面积为 0）")
    return pts


def build_scene(buildings: list[BuildingGeom],
                vegetation: list[VegetationGeom] | None = None) -> BuiltScene:
    """合并建筑与（当月启用的）植被为一个求交场景。

    vegetation 由调用方按分析月份预先过滤；本函数不感知月份口径。
    """
    meshes, owners, kinds, names = [], [], [], []
    for b in buildings:
        m = extrude_footprint(b.footprint, b.base_height, b.top_height)
        meshes.append(m)
        owners.extend([b.name] * len(m.faces))
        kinds.extend([KIND_BUILDING] * len(m.faces))
        names.append(b.name)
    for v in vegetation or []:
        m = extrude_footprint(v.footprint, 0.0, v.height)
        meshes.append(m)
        owners.extend([v.name] * len(m.faces))
        kinds.extend([KIND_VEGETATION] * len(m.faces))
        names.append(v.name)
    if not meshes:
        raise ValueError("场景至少需要一个几何体")
    combined = trimesh.util.concatenate(meshes)
    return BuiltScene(mesh=combined, face_owner=np.array(owners),
                      face_kind=np.array(kinds), names=names)


def cast_sun_ray(scene: BuiltScene, origin, direction) -> dict | None:
    """自 origin 沿 direction（指向太阳的单位向量）求最近遮挡。

    返回 None 表示无遮挡；否则返回遮挡物名称/类别/距离/命中点。
    """
    origin = np.asarray(origin, dtype=float).reshape(1, 3)
    direction = np.asarray(direction, dtype=float).reshape(1, 3)
    locations, _, tri_ids = scene.mesh.ray.intersects_location(
        origin, direction, multiple_hits=True)
    if len(locations) == 0:
        return None
    dists = np.linalg.norm(locations - origin[0], axis=1)
    i = int(np.argmin(dists))
    return {
        "occluder": str(scene.face_owner[tri_ids[i]]),
        "occluder_kind": str(scene.face_kind[tri_ids[i]]),
        "distance": float(dists[i]),
        "hit_point": [round(float(v), 4) for v in locations[i]],
    }


def rotate_footprint(footprint, angle_deg: float) -> list[tuple[float, float]]:
    """绕原点旋转 footprint（构造"坐标旋转"算例用，逆时针为正）。"""
    a = np.radians(angle_deg)
    c, s = np.cos(a), np.sin(a)
    return [(round(c * x - s * y, 6), round(s * x + c * y, 6)) for x, y in footprint]


def rotate_point(p, angle_deg: float) -> tuple[float, float]:
    return rotate_footprint([p], angle_deg)[0]

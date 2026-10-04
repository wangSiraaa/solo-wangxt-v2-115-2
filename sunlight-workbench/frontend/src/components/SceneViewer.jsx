import React, { useMemo } from 'react'
import * as THREE from 'three'
import { Canvas } from '@react-three/fiber'
import { OrbitControls, Line, Html } from '@react-three/drei'
import { toThree } from '../util.js'

const RAY_LEN = 120
const SUN_R = 160

function extrudedVolume(footprint, baseHeight, topHeight) {
  const shape = new THREE.Shape()
  footprint.forEach(([x, y], i) => (i ? shape.lineTo(x, y) : shape.moveTo(x, y)))
  const g = new THREE.ExtrudeGeometry(shape, {
    depth: topHeight - baseHeight, bevelEnabled: false,
  })
  g.translate(0, 0, baseHeight)
  g.rotateX(-Math.PI / 2) // 模型 z-up → three y-up
  return g
}

function Building({ b, highlighted, onClick }) {
  const geom = useMemo(
    () => extrudedVolume(b.footprint, b.base_height, b.top_height),
    [b],
  )
  return (
    <mesh geometry={geom} onClick={(e) => { e.stopPropagation(); onClick?.(b.name) }}>
      <meshStandardMaterial
        color={b.color}
        emissive={highlighted ? '#ff5722' : '#000'}
        emissiveIntensity={highlighted ? 0.7 : 0}
        transparent opacity={highlighted ? 0.95 : 0.85}
      />
    </mesh>
  )
}

/** 教学用植被体量：绿色半透明 + 线框树冠，与建筑的不透明盒体明确区分。
    停用月（落叶）只显示虚线框高度的线框，不参与遮挡。 */
function Vegetation({ v, active, highlighted, onClick }) {
  const geom = useMemo(
    () => extrudedVolume(v.footprint, v.base_height, v.crown_height),
    [v],
  )
  const edges = useMemo(() => new THREE.EdgesGeometry(geom), [geom])
  return (
    <group onClick={(e) => { e.stopPropagation(); onClick?.(v.name) }}>
      <mesh geometry={geom}>
        <meshStandardMaterial
          color={v.color || '#5d8f4e'}
          emissive={highlighted ? '#ff5722' : '#1b3a12'}
          emissiveIntensity={highlighted ? 0.8 : active ? 0.15 : 0}
          transparent opacity={active ? (highlighted ? 0.85 : 0.55) : 0.06}
          depthWrite={active}
        />
      </mesh>
      <lineSegments geometry={edges}>
        <lineBasicMaterial color={active ? '#a8d08d' : '#6c8a5e'}
          transparent opacity={active ? 0.95 : 0.55} />
      </lineSegments>
      {!active && (
        <Html position={toThree(
          [v.footprint[0][0], v.footprint[0][1], v.crown_height])} center>
          <div className="veg-off-label">落叶停用</div>
        </Html>
      )}
    </group>
  )
}

function MeasurePoint({ p, status, selected, onClick }) {
  const color = selected ? '#ff4081' : status === 'shaded' ? '#4a6fa5'
    : status === 'sunlit' ? '#f6c453' : '#999'
  return (
    <mesh position={toThree(p.position)}
      onClick={(e) => { e.stopPropagation(); onClick?.(p.id) }}>
      <sphereGeometry args={[0.45, 16, 16]} />
      <meshStandardMaterial color={color} />
    </mesh>
  )
}

/** 真北箭头：模型 +y 轴（= 场景声明的模型北），红色；真北由 north_offset 决定，
    场景页已统一口径，这里画模型北并标注偏角。 */
function NorthArrow() {
  return (
    <group position={[0, 0.2, 0]}>
      <Line points={[toThree([0, 0, 0]), toThree([0, 18, 0])]} color="#d32f2f" lineWidth={3} />
      <Html position={toThree([0, 20, 0])} center>
        <div className="north-label">模型北 ↑</div>
      </Html>
    </group>
  )
}

function occluderTypeOf(payload, name) {
  if (payload?.vegetation?.some((v) => v.name === name)) return 'vegetation'
  return 'building'
}

export default function SceneViewer({
  payload, sunpath, timeIdx, pointStatus, selectedPointId,
  highlightOccluder, activeMonth, onSelectPoint, onSelectBuilding,
}) {
  const sun = sunpath?.points?.[timeIdx]
  const sunPos = sun ? toThree(sun.dir.map((v) => v * SUN_R)) : null
  return (
    <Canvas camera={{ position: [60, 70, 90], up: [0, 1, 0], fov: 45 }}
      style={{ background: '#10141c' }}>
      <ambientLight intensity={0.5} />
      <directionalLight position={sunPos ?? [50, 80, 30]} intensity={1.2} />
      <gridHelper args={[200, 40, '#2a3242', '#1c2330']} rotation={[0, 0, 0]} />
      <NorthArrow />
      {payload?.buildings.map((b) => (
        <Building key={`b-${b.id}`} b={b}
          highlighted={highlightOccluder === b.name}
          onClick={onSelectBuilding} />
      ))}
      {payload?.vegetation?.map((v) => (
        <Vegetation key={`v-${v.id}`} v={v}
          active={activeMonth != null && v.leaf_months.includes(activeMonth)}
          highlighted={highlightOccluder === v.name}
          onClick={onSelectBuilding} />
      ))}
      {payload?.points.map((p) => (
        <MeasurePoint key={p.id} p={p}
          status={pointStatus?.[p.id]?.status}
          selected={p.id === selectedPointId}
          onClick={onSelectPoint} />
      ))}
      {/* 太阳路径（当日，模型坐标系） */}
      {sunpath && (
        <Line points={sunpath.points.map((s) => toThree(s.dir.map((v) => v * SUN_R)))}
          color="#f6c453" dashed dashSize={2} gapSize={1} />
      )}
      {sunPos && (
        <mesh position={sunPos}>
          <sphereGeometry args={[3, 16, 16]} />
          <meshBasicMaterial color="#ffeb3b" />
        </mesh>
      )}
      {/* 测点→太阳 射线：绿=晒到，红=被遮挡（遮挡物类型见射线标签/右侧面板） */}
      {sun && payload?.points.map((p) => {
        const st = pointStatus?.[p.id]
        if (!st || st.status === 'night') return null
        const o = p.position
        const e = [o[0] + sun.dir[0] * RAY_LEN, o[1] + sun.dir[1] * RAY_LEN,
                   o[2] + sun.dir[2] * RAY_LEN]
        return (
          <group key={p.id}>
            <Line points={[toThree(o), toThree(e)]}
              color={st.status === 'sunlit' ? '#7ce38b' : '#ef5350'} lineWidth={2} />
            {st.status === 'shaded' && st.occluder && (
              <Html position={toThree([
                (o[0] + e[0]) / 2, (o[1] + e[1]) / 2, (o[2] + e[2]) / 2])} center>
                <div className={`ray-tag ${occluderTypeOf(payload, st.occluder)}`}>
                  {occluderTypeOf(payload, st.occluder) === 'vegetation' ? '🌳 ' : '🏢 '}
                  {st.occluder}
                </div>
              </Html>
            )}
          </group>
        )
      })}
      <OrbitControls makeDefault />
    </Canvas>
  )
}

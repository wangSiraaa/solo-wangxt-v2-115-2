import React, { useState } from 'react'

const MONTHS = Array.from({ length: 12 }, (_, i) => i + 1)

/** 解析 footprint 文本：每行 "x, y"，至少 3 个点。 */
export function parseFootprint(text) {
  const pts = text
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => line.split(/[,，\s]+/).filter(Boolean).map(Number))
  if (pts.length < 3 || pts.some((p) => p.length !== 2 || p.some(Number.isNaN))) {
    throw new Error('多边形需要至少 3 行 "x, y"（模型局部米制坐标）')
  }
  return pts
}

export function footprintText(fp) {
  return fp.map(([x, y]) => `${x}, ${y}`).join('\n')
}

function MonthPicker({ value, onChange }) {
  const toggle = (m) => {
    const set = new Set(value)
    set.has(m) ? set.delete(m) : set.add(m)
    onChange([...set].sort((a, b) => a - b))
  }
  return (
    <div className="month-picker">
      {MONTHS.map((m) => (
        <button key={m} type="button"
          className={`month-btn ${value.includes(m) ? 'on' : ''}`}
          onClick={() => toggle(m)}>{m}月</button>
      ))}
    </div>
  )
}

function VegForm({ initial, submitLabel, onSubmit, onCancel }) {
  const [name, setName] = useState(initial?.name ?? '')
  const [baseHeight, setBaseHeight] = useState(initial?.base_height ?? 2)
  const [crownHeight, setCrownHeight] = useState(initial?.crown_height ?? 9)
  const [months, setMonths] = useState(initial?.leaf_months ?? [4, 5, 6, 7, 8, 9, 10])
  const [fpText, setFpText] = useState(
    initial ? footprintText(initial.footprint) : '17.5, -2.5\n22.5, -2.5\n22.5, 2.5\n17.5, 2.5')
  const [err, setErr] = useState(null)

  const submit = async () => {
    setErr(null)
    try {
      const footprint = parseFootprint(fpText)
      await onSubmit({
        name: name.trim(), footprint,
        base_height: Number(baseHeight), crown_height: Number(crownHeight),
        leaf_months: months,
      })
    } catch (e) {
      setErr(e.message || String(e))
    }
  }

  return (
    <div className="veg-form">
      <input placeholder="名称，如 V3_西侧庭荫树" value={name}
        onChange={(e) => setName(e.target.value)} />
      <div className="veg-row">
        <label>枝下高 <input type="number" step="0.1" value={baseHeight}
          onChange={(e) => setBaseHeight(e.target.value)} /></label>
        <label>树冠顶 <input type="number" step="0.1" value={crownHeight}
          onChange={(e) => setCrownHeight(e.target.value)} /></label>
      </div>
      <label>参与遮挡的月份（有叶月，停用月按落叶处理）</label>
      <MonthPicker value={months} onChange={setMonths} />
      <textarea rows={4} value={fpText} onChange={(e) => setFpText(e.target.value)}
        title='每行一个顶点 "x, y"' />
      {err && <div className="error small">{err}</div>}
      <div className="veg-row">
        <button onClick={submit}>{submitLabel}</button>
        {onCancel && <button onClick={onCancel}>取消</button>}
      </div>
    </div>
  )
}

/** 教学用植被遮挡物管理：合成树冠体量，跟随场景快照冻结。 */
export default function VegetationPanel({
  sceneId, vegetation, activeMonth, readOnly, onChanged,
}) {
  const [adding, setAdding] = useState(false)
  const [editingId, setEditingId] = useState(null)
  const [err, setErr] = useState(null)

  const refresh = async () => {
    setAdding(false); setEditingId(null)
    try { await onChanged() } catch (e) { setErr(String(e)) }
  }

  const add = async (body) => {
    setErr(null)
    try {
      const { api } = await import('../api.js')
      await api.addVegetation(sceneId, body)
      await refresh()
    } catch (e) { setErr(String(e)) }
  }
  const update = async (vegId, body) => {
    setErr(null)
    try {
      const { api } = await import('../api.js')
      await api.updateVegetation(sceneId, vegId, body)
      await refresh()
    } catch (e) { setErr(String(e)) }
  }
  const remove = async (vegId) => {
    setErr(null)
    try {
      const { api } = await import('../api.js')
      await api.deleteVegetation(sceneId, vegId)
      await refresh()
    } catch (e) { setErr(String(e)) }
  }

  return (
    <div className="veg-panel">
      <h4>教学用植被遮挡物（合成树冠体量）</h4>
      <div className="muted small">
        与建筑同为局部坐标多边形体量；仅在所选日期的有叶月参与射线，
        停用月按落叶处理。运行时冻结进快照，编辑不影响旧运行。
      </div>
      {vegetation?.map((v) => (
        <div key={v.id} className={`veg-item ${
          activeMonth != null && v.leaf_months.includes(activeMonth) ? 'active' : 'inactive'}`}>
          {editingId === v.id ? (
            <VegForm initial={v} submitLabel="保存修改"
              onSubmit={(body) => update(v.id, body)}
              onCancel={() => setEditingId(null)} />
          ) : (
            <>
              <div>🌳 {v.name}
                <span className="veg-state">
                  {activeMonth != null && v.leaf_months.includes(activeMonth)
                    ? `${activeMonth}月启用·参与遮挡` : '当月停用·落叶'}
                </span>
              </div>
              <div className="muted small">
                冠顶 {v.crown_height}m · 枝下高 {v.base_height}m ·
                有叶月 {v.leaf_months.join(',')}
              </div>
              {!readOnly && (
                <div className="veg-row">
                  <button onClick={() => setEditingId(v.id)}>编辑</button>
                  <button onClick={() => remove(v.id)}>删除</button>
                </div>
              )}
            </>
          )}
        </div>
      ))}
      {!readOnly && !adding && (
        <button onClick={() => setAdding(true)}>＋ 新增教学植被</button>
      )}
      {adding && (
        <VegForm submitLabel="保存植被" onSubmit={add}
          onCancel={() => setAdding(false)} />
      )}
      {readOnly && (
        <div className="muted small">正在查看历史运行快照，植被为冻结状态，不可编辑。</div>
      )}
      {err && <div className="error small">{err}</div>}
    </div>
  )
}

import React, { useState } from 'react'
import { api } from '../api.js'

const MONTHS = Array.from({ length: 12 }, (_, i) => i + 1)

/** 解析顶点文本：每行或分号分隔一个 "x,y" 顶点。 */
function parseFootprint(text) {
  return text.split(/[;\n]/).map((s) => s.trim()).filter(Boolean)
    .map((s) => s.split(/[,\s]+/).filter(Boolean).map(Number))
}

const EMPTY_FORM = {
  name: '', height: 8,
  footprint: '10,-15\n17,-15\n17,-9\n10,-9',
  months: [4, 5, 6, 7, 8, 9, 10],
}

/** 教学用植被管理：多边形 footprint + 高度 + 启用月份。
    保存即触发场景刷新；历史运行的快照不受影响（服务端按快照追溯）。 */
export default function VegetationPanel({ sceneId, vegetation, currentMonth, onChanged }) {
  const [editing, setEditing] = useState(null) // null | 'new' | 植被id
  const [form, setForm] = useState(EMPTY_FORM)
  const [error, setError] = useState(null)

  const startNew = () => {
    setEditing('new'); setForm(EMPTY_FORM); setError(null)
  }
  const startEdit = (v) => {
    setEditing(v.id)
    setForm({
      name: v.name, height: v.height,
      footprint: v.footprint.map((p) => `${p[0]},${p[1]}`).join('\n'),
      months: [...v.active_months],
    })
    setError(null)
  }
  const toggleMonth = (m) => {
    setForm((f) => ({
      ...f,
      months: f.months.includes(m)
        ? f.months.filter((x) => x !== m)
        : [...f.months, m].sort((a, b) => a - b),
    }))
  }

  const submit = async () => {
    setError(null)
    const body = {
      name: form.name.trim(),
      height: +form.height,
      footprint: parseFootprint(form.footprint),
      active_months: form.months,
    }
    try {
      if (editing === 'new') await api.createVegetation(sceneId, body)
      else await api.updateVegetation(editing, body)
      setEditing(null)
      onChanged?.()
    } catch (e) {
      setError(String(e.message || e))
    }
  }

  const remove = async (id) => {
    setError(null)
    try {
      await api.deleteVegetation(id)
      onChanged?.()
    } catch (e) {
      setError(String(e.message || e))
    }
  }

  return (
    <div className="veg-panel">
      <label>教学用植被（合成几何 · 按月份参与遮挡）</label>
      {vegetation.map((v) => (
        <div key={v.id} className="veg-item">
          <div>
            <b>{v.name}</b>
            <span className={`veg-tag ${v.active_months.includes(currentMonth) ? 'on' : 'off'}`}>
              {currentMonth}月{v.active_months.includes(currentMonth) ? '启用' : '停用'}
            </span>
          </div>
          <div className="muted small">
            高 {v.height} m · 启用月份 {v.active_months.join('/')}
          </div>
          <div className="veg-actions">
            <button onClick={() => startEdit(v)}>编辑</button>
            <button onClick={() => remove(v.id)}>删除</button>
          </div>
        </div>
      ))}
      {!vegetation.length && !editing && (
        <div className="muted small">暂无植被。可添加简化树冠体量，演示夏荫/冬落叶差别。</div>
      )}
      {editing ? (
        <div className="veg-form">
          <input value={form.name} placeholder="名称（须与建筑/植被不重名）"
            onChange={(e) => setForm({ ...form, name: e.target.value })} />
          <label>高度（米，自地面起算）</label>
          <input type="number" min="0.1" step="0.5" value={form.height}
            onChange={(e) => setForm({ ...form, height: e.target.value })} />
          <label>footprint 顶点（每行一个 x,y，模型局部坐标）</label>
          <textarea rows={4} value={form.footprint}
            onChange={(e) => setForm({ ...form, footprint: e.target.value })} />
          <label>参与遮挡的月份</label>
          <div className="months">
            {MONTHS.map((m) => (
              <button key={m} type="button"
                className={form.months.includes(m) ? 'on' : ''}
                onClick={() => toggleMonth(m)}>{m}</button>
            ))}
          </div>
          <div className="veg-actions">
            <button onClick={submit}>{editing === 'new' ? '添加' : '保存'}</button>
            <button onClick={() => setEditing(null)}>取消</button>
          </div>
        </div>
      ) : (
        <button onClick={startNew}>添加植被</button>
      )}
      {error && <div className="error">{error}</div>}
    </div>
  )
}

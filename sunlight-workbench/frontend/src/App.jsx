import React, { useEffect, useMemo, useState } from 'react'
import { api } from './api.js'
import SceneViewer from './components/SceneViewer.jsx'
import ResultsPanel from './components/ResultsPanel.jsx'
import VegetationPanel from './components/VegetationPanel.jsx'

const DATES = ['2026-01-15', '2026-03-20', '2026-07-15'] // 跨冬夏算例日期

export default function App() {
  const [scenes, setScenes] = useState([])
  const [sceneId, setSceneId] = useState(null)
  const [payload, setPayload] = useState(null)   // 实时场景或历史快照内容
  const [mode, setMode] = useState('live')       // live=可编辑场景；snapshot=旧运行冻结
  const [date, setDate] = useState(DATES[0])
  const [sunpath, setSunpath] = useState(null)
  const [timeIdx, setTimeIdx] = useState(30)
  const [run, setRun] = useState(null)
  const [selectedPointId, setSelectedPointId] = useState(null)
  const [highlightOccluder, setHighlightOccluder] = useState(null)
  const [trace, setTrace] = useState(null)
  const [error, setError] = useState(null)

  const activeMonth = Number(date.slice(5, 7))
  // 查看历史运行时，植被启用态按运行冻结时的月份渲染（而非当前选的日期）
  const viewMonth = mode === 'snapshot' ? (run?.params?.month ?? activeMonth) : activeMonth

  useEffect(() => { api.scenes().then(setScenes).catch((e) => setError(String(e))) }, [])

  const loadLive = (id, d) => {
    setMode('live')
    setRun(null); setSelectedPointId(null); setTrace(null); setHighlightOccluder(null)
    api.scene(id).then(setPayload)
    api.sunpath(id, d).then(setSunpath)
  }

  useEffect(() => {
    if (!sceneId) return
    loadLive(sceneId, date)
  }, [sceneId, date])

  const doSeed = async () => { await api.seed(); setScenes(await api.scenes()) }

  const doRun = async () => {
    setError(null)
    const r = await api.run(sceneId, date, 5)
    const full = await api.runResult(r.run_id)
    setRun(full)
    // 结果关联快照：渲染切换到快照内容，保证结果-场景一致可追溯
    const snap = await api.snapshot(full.snapshot_id)
    setPayload(snap.payload)
    setMode('snapshot')
  }

  // 当前时刻各测点状态（取最近细样本）
  const pointStatus = useMemo(() => {
    if (!run || !sunpath) return null
    const t = sunpath.points[timeIdx]?.time
    if (!t) return null
    const out = {}
    for (const res of run.results) {
      let best = null
      for (const s of res.fine_samples) {
        if (!best || Math.abs(s.time.localeCompare(t)) < Math.abs(best.time.localeCompare(t))) best = s
      }
      out[res.point_id] = best
    }
    return out
  }, [run, sunpath, timeIdx])

  const selectedResult = run?.results.find((r) => r.point_id === selectedPointId)

  const doTrace = async (pointId) => {
    setTrace(await api.trace(run.run_id, pointId))
  }

  return (
    <div className="layout">
      <header>
        <b>日照分析工作台</b>
        <span className="badge">合成场景 · 示例评价口径 · 非规划合规结论</span>
      </header>
      <aside>
        <label>场景（坐标基准统一挂在场景上）</label>
        <select value={sceneId ?? ''} onChange={(e) => setSceneId(+e.target.value)}>
          <option value="" disabled>选择场景</option>
          {scenes.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
        </select>
        {!scenes.length && <button onClick={doSeed}>初始化合成场景</button>}
        {payload && (
          <div className="meta">
            <div>经纬度 {payload.scene.latitude}, {payload.scene.longitude}</div>
            <div>时区 {payload.scene.timezone}</div>
            <div>模型北偏角 {payload.scene.north_offset_deg}°</div>
            <div className="warn">未建模遮挡：{payload.scene.unmodeled_occluders}</div>
          </div>
        )}
        <label>日期（跨冬夏算例；植被按当月是否有叶启用）</label>
        <select value={date} onChange={(e) => setDate(e.target.value)} disabled={mode === 'snapshot'}>
          {DATES.map((d) => <option key={d}>{d}</option>)}
        </select>
        <button disabled={!sceneId} onClick={doRun}>运行当日分析（5min 步长）</button>
        {run && <div className="muted small">run #{run.run_id} · 快照 #{run.snapshot_id}</div>}
        {mode === 'snapshot' && (
          <div className="snapshot-bar">
            <span className="muted small">正在按运行时快照展示（植被已冻结）</span>
            <button onClick={() => loadLive(sceneId, date)}>返回实时场景编辑植被</button>
          </div>
        )}
        {error && <div className="error">{error}</div>}
        {sunpath && (
          <>
            <label>时刻 {sunpath.points[timeIdx]?.time.slice(11, 16)}</label>
            <input type="range" min={0} max={sunpath.points.length - 1}
              value={timeIdx} onChange={(e) => setTimeIdx(+e.target.value)} />
          </>
        )}
        {payload && sceneId && (
          <VegetationPanel
            sceneId={sceneId}
            vegetation={payload.vegetation ?? []}
            activeMonth={viewMonth}
            readOnly={mode === 'snapshot'}
            onChanged={() => api.scene(sceneId).then(setPayload)} />
        )}
        {trace && (
          <div className="trace">
            <h4>遮挡物追查（测点 #{trace.point_id}）</h4>
            <div>遮挡物：{trace.occluders.length
              ? trace.occluders.map((o) => `${trace.occluder_types[o] === 'vegetation' ? '🌳' : '🏢'}${o}`).join('、')
              : '无'}</div>
            <div className="muted small">{trace.note}</div>
            <button onClick={() => setTrace(null)}>关闭</button>
          </div>
        )}
      </aside>
      <main>
        <SceneViewer
          payload={payload} sunpath={sunpath} timeIdx={timeIdx}
          pointStatus={pointStatus} selectedPointId={selectedPointId}
          highlightOccluder={highlightOccluder} activeMonth={viewMonth}
          onSelectPoint={setSelectedPointId}
          onSelectBuilding={setHighlightOccluder} />
      </main>
      <aside className="right">
        <ResultsPanel
          run={run} result={selectedResult}
          onHoverInterval={(iv) => setHighlightOccluder(iv?.occluder ?? null)}
          onTrace={doTrace} />
      </aside>
    </div>
  )
}

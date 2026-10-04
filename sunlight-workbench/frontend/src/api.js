const BASE = '/api'

async function req(path, options) {
  const r = await fetch(BASE + path, options)
  if (!r.ok) throw new Error(`${r.status}: ${await r.text()}`)
  return r.json()
}

const sendJson = (method, path, body) => req(path, {
  method,
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body),
})

export const api = {
  scenes: () => req('/scenes'),
  seed: () => req('/scenes/seed', { method: 'POST' }),
  scene: (id) => req(`/scenes/${id}`),
  sunpath: (id, date) => req(`/scenes/${id}/sunpath?date=${date}`),
  run: (scene_id, date, step_minutes = 5) =>
    sendJson('POST', '/analysis/run', { scene_id, date, step_minutes }),
  runResult: (runId) => req(`/analysis/${runId}`),
  trace: (runId, pointId, time) =>
    req(`/analysis/${runId}/points/${pointId}/trace` + (time ? `?time=${time}` : '')),
  snapshot: (id) => req(`/snapshots/${id}`),
  // 教学用植被遮挡物
  vegetation: (sceneId) => req(`/scenes/${sceneId}/vegetation`),
  createVegetation: (sceneId, body) =>
    sendJson('POST', `/scenes/${sceneId}/vegetation`, body),
  updateVegetation: (id, body) => sendJson('PUT', `/vegetation/${id}`, body),
  deleteVegetation: (id) => req(`/vegetation/${id}`, { method: 'DELETE' }),
}

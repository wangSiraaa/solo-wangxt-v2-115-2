const BASE = '/api'

async function req(path, options) {
  const r = await fetch(BASE + path, options)
  if (!r.ok) throw new Error(`${r.status}: ${await r.text()}`)
  return r.json()
}

const jsonOpts = (method, body) => ({
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
    req('/analysis/run', jsonOpts('POST', { scene_id, date, step_minutes })),
  runResult: (runId) => req(`/analysis/${runId}`),
  trace: (runId, pointId, time) =>
    req(`/analysis/${runId}/points/${pointId}/trace` + (time ? `?time=${time}` : '')),
  snapshot: (id) => req(`/snapshots/${id}`),
  addVegetation: (sceneId, veg) =>
    req(`/scenes/${sceneId}/vegetation`, jsonOpts('POST', veg)),
  updateVegetation: (sceneId, vegId, patch) =>
    req(`/scenes/${sceneId}/vegetation/${vegId}`, jsonOpts('PUT', patch)),
  deleteVegetation: (sceneId, vegId) =>
    req(`/scenes/${sceneId}/vegetation/${vegId}`, { method: 'DELETE' }),
}

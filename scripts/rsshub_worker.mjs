// Minimal skill-owned RSSHub worker.
// Starts an HTTP server on 127.0.0.1:${RSSHUB_PORT} that proxies requests to
// the rsshub npm package (installed in SKILL/node_modules/rsshub).
//
// Companion to scripts/rsshub_manager.py — the manager spawns this as a
// detached node process. Keeping the logic here minimal; all control (start /
// stop / health / upgrade) lives in the Python manager.

import { createServer } from 'node:http'
import { fileURLToPath } from 'node:url'
import { readFile } from 'node:fs/promises'
import { init } from 'rsshub'

const PORT = Number.parseInt(process.env.RSSHUB_PORT || '1201', 10)
const HOST = '127.0.0.1'

async function resolveRsshubApp () {
  // The rsshub package entry is a tiny shim that dynamically imports a
  // versioned app-*.mjs. Read the shim and pull out the target path so we
  // can import the actual hono app that exposes request().
  const pkgEntryUrl = await import.meta.resolve('rsshub')
  const pkgSource = await readFile(fileURLToPath(pkgEntryUrl), 'utf8')
  const match = pkgSource.match(/import\("(\.\/app-[^"]+\.mjs)"\)/)
  if (!match?.[1]) {
    throw new Error('Cannot resolve RSSHub app module from package entry')
  }
  const appUrl = new URL(match[1], pkgEntryUrl).href
  const mod = await import(appUrl)
  if (!mod?.default?.request) {
    throw new Error('RSSHub app module is missing request()')
  }
  return mod.default
}

async function forwardToNode (res, response) {
  res.statusCode = response.status
  response.headers.forEach((value, key) => {
    if (key.toLowerCase() === 'content-length') return
    res.setHeader(key, value)
  })
  const body = Buffer.from(await response.arrayBuffer())
  res.end(body)
}

async function main () {
  await init({
    IS_PACKAGE: true,
    CACHE_TYPE: 'memory',
    ALLOW_ORIGIN: '*',
    NODE_ENV: 'production',
  })

  const app = await resolveRsshubApp()

  const server = createServer(async (req, res) => {
    try {
      const reqUrl = new URL(req.url || '/', `http://${HOST}:${PORT}`)

      if (reqUrl.pathname === '/healthz') {
        res.statusCode = 200
        res.setHeader('content-type', 'application/json; charset=utf-8')
        res.end(JSON.stringify({ ok: true, service: 'lets-go-rss-rsshub', port: PORT }))
        return
      }
      if (reqUrl.pathname === '/shutdown' && req.method === 'POST') {
        res.statusCode = 202
        res.end('{"ok":true}')
        setTimeout(() => process.exit(0), 50)
        return
      }

      const headers = new Headers()
      for (const [k, v] of Object.entries(req.headers)) {
        const lk = k.toLowerCase()
        if (lk === 'host' || lk === 'connection' || lk === 'content-length') continue
        if (Array.isArray(v)) headers.set(k, v.join(', '))
        else if (typeof v === 'string') headers.set(k, v)
      }

      let body
      if (req.method !== 'GET' && req.method !== 'HEAD') {
        const chunks = []
        for await (const chunk of req) chunks.push(chunk)
        const buf = Buffer.concat(chunks)
        if (buf.length > 0) body = buf
      }

      const ctrl = new AbortController()
      const t = setTimeout(() => ctrl.abort(), 30_000)
      const response = await app.request(`${reqUrl.pathname}${reqUrl.search}`, {
        method: req.method || 'GET',
        headers,
        body,
        signal: ctrl.signal,
      })
      clearTimeout(t)
      await forwardToNode(res, response)
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e)
      console.error(`[rsshub-worker:request] ${msg}`)
      res.statusCode = 500
      res.setHeader('content-type', 'application/json; charset=utf-8')
      res.end(JSON.stringify({ error: msg }))
    }
  })

  server.on('error', (e) => {
    console.error(`[rsshub-worker:server] ${e.message}`)
    process.exit(1)
  })

  server.listen(PORT, HOST, () => {
    console.error(`[rsshub-worker] listening on ${HOST}:${PORT}`)
  })
}

process.on('uncaughtException', (e) => {
  // Node best practice: bail on uncaught exceptions (state may be corrupt);
  // the Python manager will restart us.
  console.error(`[rsshub-worker:uncaught] ${e?.message || e}`)
  process.exit(1)
})
process.on('unhandledRejection', (e) => {
  // Many RSSHub routes throw in unexpected places (e.g. bad JSON upstream);
  // log and continue rather than killing the whole worker.
  console.error(`[rsshub-worker:unhandled] ${e?.message || e}`)
})

main().catch((e) => {
  console.error(`[rsshub-worker:start] ${e?.message || e}`)
  process.exit(1)
})

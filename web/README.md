# Web UI

React + Vite + Tailwind. Built to static assets and served by the FastAPI app,
so the deployed stack is single-origin (no CORS) and one container fewer.

## Development

```bash
npm ci
npm run dev        # http://localhost:5173, proxies /v1 to the API on :8123
```

## Build

```bash
npm run build      # -> web/dist, picked up automatically by the API
```

`deploy/Dockerfile` runs this build in a node stage and copies `dist` into the
Python image, so a Docker build needs no local Node install.

## Structure

| File | Purpose |
| --- | --- |
| `src/api.ts` | Typed client. JWT held **in memory only**; tenant/role decoded from claims. No request ever sends `tenant_id`. |
| `src/App.tsx` | Shell: sign-in, console/analytics tabs, run metadata |
| `src/components/Progress.tsx` | Staged progress for 30-70s cold runs |
| `src/components/Answer.tsx` | Answer + the distinct abstained / degraded / blocked states |
| `src/components/EvidenceDrawer.tsx` | Citation → source, authority, agent, snippet |
| `src/components/Csat.tsx` | 1-5 rating posted against the run_id |
| `src/components/Analytics.tsx` | Dashboard; org switcher renders for `platform_admin` only |

## Roles

The org switcher appears only for `platform_admin`. A tenant `admin` sees a
fixed scope chip instead — not a disabled switcher, and no list of other tenant
names. This is an affordance, not the boundary: the server pins any non-platform
role to its own tenant regardless of what the client requests.

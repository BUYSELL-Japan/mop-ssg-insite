# mop-insite-proxy

Cloudflare Worker that routes `mop-insite.com/*` to the three Cloudflare Pages projects.
This folder is the source of truth (until 2026-11-15 it only existed in the Cloudflare dashboard).

| Path (prefix match, case-insensitive) | Pages project |
|---|---|
| `/en/spot*`, `/zh/spot*`, `/en/restaurant*`, `/zh/restaurant*`, `/_ssg_assets/*`, `/sitemap.xml`, `/robots.txt` | `mop-ssg-insite` (this repo) |
| `/en/guidebook*`, `/zh/guidebook*`, `/_guidebook_assets/*`, `/_astro/*`, `/sitemap-index.xml`, `/sitemap-0.xml` | `mop-guidebook-insite` |
| everything else (incl. `/sw.js`, `/workbox-*`, `/assets/*`) | `mop-insite` (map app) |

This Worker is **not** deployed by the Pages build. Deploy it manually:

```sh
cd worker
npm ci
npx wrangler deploy            # npx wrangler deploy --dry-run to check first
npx wrangler rollback          # revert to the previous version if needed
```

When adding a new top-level path served by SSG or Guidebook, also add it to
`navigateFallbackDenylist` in MOP-Okinawa `vite.config.ts`, or the installed PWA
will answer the path with the map app.

Do not edit the Worker in the dashboard; if someone does, re-fetch it here first:

```sh
npm_config_legacy_peer_deps=true npx create-cloudflare@2 mop-insite-proxy --type pre-existing \
  --existing-script mop-insite-proxy --no-git --no-deploy --lang js -y
```

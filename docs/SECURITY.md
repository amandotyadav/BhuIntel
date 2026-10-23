# Security

| control | implementation |
|---|---|
| authentication | local accounts, argon2id password hashes (`argon2-cffi`), HS256 JWT bearer tokens (`BHU_SECRET_KEY` ≥ 32 chars, refuses dev default in production), configurable lifetime |
| authorisation | global roles `viewer < analyst < reviewer < admin`, per-project membership caps; projects the user cannot access return 404; every endpoint checks project scope |
| legal safeguards | legally sensitive approvals require admin + explicit acknowledgement; cadastral geometry is never auto-modified beyond numerically identical repairs |
| audit | append-only `audit_events` (hash chain, `GET /audit/verify`) and `review_decisions`/`integrated_feature_history` (DB triggers reject UPDATE/DELETE) |
| uploads | streamed to storage with a size cap; extension allow-list; content validated by GDAL; ZIP: path traversal, absolute paths, symlinks, encrypted members, member count, total size and compression-ratio (zip bomb) checks; extraction into temp dirs only |
| SQL injection | SQLAlchemy bound parameters everywhere; the few dynamic SQL fragments are built from server-side constants, never from user input |
| personal data | owner-name-like attributes redacted for viewer/analyst roles; never copied into the integrated layer, OGC API or exports; not logged; fixture uses synthetic names only |
| transport/browser | CORS allow-list from env; nginx adds `X-Content-Type-Options`, `X-Frame-Options: DENY`, `Referrer-Policy`; API adds nosniff/referrer headers |
| abuse | per-IP rate limits (global and stricter for login); job timeouts; feature/pixel/upload/archive limits |
| secrets | only via environment (`.env`, not committed); `.env.example` has placeholders |
| containers | non-root user in API/worker image, minimal slim base, healthchecks, resource limits in compose |
| model artefacts | joblib artefacts are produced by the service itself and SHA-256-verified before loading (pickle-based formats must never be loaded from untrusted sources) |

## Token storage in the browser

The SPA keeps the JWT in `sessionStorage` (cleared when the tab closes). This is vulnerable to token
theft if an XSS bug existed. React escapes rendered content and no HTML from data is injected, but
production deployments behind SSO should prefer an httpOnly, SameSite cookie session.

## Dependency audit (run 2026-10-23)

* `pip-audit` (backend environment): **no known vulnerabilities**.
* `npm audit --omit=dev` (shipped frontend dependencies): **0 vulnerabilities** after upgrading
  `react-router-dom` 6.30 → 7.18.4 (moderate open-redirect advisory).
* `npm audit` including dev tooling: 7 findings (5 high, 2 moderate), all in Tailwind CSS 3's build-time
  file-globbing chain (`braces`, `micromatch`, `chokidar`, `fast-glob`, `postcss-selector-parser`).
  No patched `braces` release exists for that range. Exposure is limited to the developer/build
  machine processing our own static Tailwind config; nothing ships to users. Remediation path:
  migrate to Tailwind 4. The earlier critical Vitest/Tinypool advisories were fixed by upgrading
  Vitest to 4.1.12.

## Reporting

There is no public disclosure channel configured yet; add one (e.g. `SECURITY.md` contact) before
deployment.

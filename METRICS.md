# Quality Metrics

How security, usability, and performance are measured, following the **ISO/IEC 25010** quality model.

**Blocking** means CI fails if the target is not met. **Report-only** means the value is measured on every run and shown in the CI run summary (the `performance` job, output in `$GITHUB_STEP_SUMMARY`), but never fails CI.

## Security

| Metric | Target | Reference | Where measured | Type |
|---|---|---|---|---|
| Application security requirements | Level 2 | OWASP ASVS 5.0 | Reviews, tests in `tests/` | Guideline |
| Most common risks covered | All 10 | OWASP Top 10 (2025) | Reviews, CodeQL | Guideline |
| Static-analysis findings | 0 open | CodeQL (default setup) | Security → Code scanning | Report-only |
| Workflow findings | 0 at or above “medium” | zizmor | CI job `workflows` | Blocking |
| Known dependency vulnerabilities | 0 | pip-audit, Dependency Review | CI jobs `audit`, `dependency-review` | Blocking |
| Code security-rule findings | 0 | ruff `S` (Bandit) | CI job `test` | Blocking |
| Secrets in repository | 0 | gitleaks | CI job `secrets` | Blocking |
| Repository security score | ≥ 7/10 | OpenSSF Scorecard | `scorecard.yml`, Security → Code scanning | Report-only |
| Build security | Build L2 | SLSA | Build provenance in `release.yml` | Achieved |
| systemd unit hardening | ≤ 2.0 | `systemd-analyze security` | `deploy/` | Guideline |
| Container: will not start without key; non-root; read-only; nothing readable in `/data` | Pass | CIS Docker Benchmark (selected controls) | CI job `docker` (`scripts/docker_smoke.sh`) | Blocking |
| Container image signed and provenance-attested | Every release | Sigstore Cosign, SLSA | `release.yml`, job `image` | Achieved |
| Time to remediate vulnerabilities | Critical ≤ 7 days; high ≤ 30 days | Common remediation targets | Security Advisories, Dependabot | Guideline |

## Usability (UI)

| Metric | Target | Reference | Where measured | Type |
|---|---|---|---|---|
| Accessibility | 0 violations | WCAG 2.2 AA (EN 301 549, BFSG), axe-core | `tests/test_ui_browser.py`: every page, light/dark, mobile/desktop | Blocking |
| Text contrast | ≥ 4.5:1 | WCAG 2.2 AA | axe-core in the same test | Blocking |
| No inline styles or scripts | 0 CSP violations | Custom CSP without `unsafe-inline` | Browser console in the same test | Blocking |
| Mobile-first layout | No page wider than 375 px | Project target (#72) | Same test | Blocking |
| Feedback | Immediate ≤ 0.1 s; uninterrupted ≤ 1 s; progress indicator from 10 s | Nielsen | Background jobs with progress | Guideline |

axe-core automatically detects roughly half of WCAG issues. Keyboard navigation and screen-reader behavior are checked manually for major UI changes.

## Performance

Measured by `tests/perf_service.py` on the CI runner, without real network access. Values vary by runner; trends between runs matter most.

| Metric | Target | Reference | Type |
|---|---|---|---|
| TTFB (server response time), slowest page | ≤ 800 ms | Core Web Vitals | Report-only |
| TTFB, median across pages | ≤ 200 ms | Nielsen 0.1 s | Report-only |
| LCP (largest contentful paint), slowest page | ≤ 2.5 s | Core Web Vitals | Report-only |
| CLS (layout shift), worst page | ≤ 0.1 | Core Web Vitals | Report-only |
| INP (interaction to next paint) | ≤ 200 ms | Core Web Vitals | N/A: the UI has no JavaScript |
| Transferred data, largest page | ≤ 500 KiB | Page weight | Report-only |
| X-Ray with large portfolio (15 ETFs, 13,500 holdings): median response time | ≤ 800 ms | Core Web Vitals TTFB | Report-only |
| X-Ray with large portfolio: page weight | ≤ 500 KiB | Page weight | Report-only |
| Sync duration (300 bookings, 3,000 existing activities) | ≤ 10 s | Nielsen; progress indicator thereafter | Report-only |
| Wealthfolio requests per booking | ≤ 4 | Internal budget | Report-only |
| Read-only Wealthfolio requests | No per-booking request (N+1) | Internal budget | Report-only |
| SQL statements per booking | ≤ 10 | Internal budget | Report-only |
| Queries without an index | 0 | `EXPLAIN QUERY PLAN` | Report-only |
| Duplicate detection per booking with 3,000 existing activities (hourly over ~4 months) | ≤ 10 ms | Internal budget | Report-only |

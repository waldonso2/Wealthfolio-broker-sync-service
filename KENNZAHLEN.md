# Kennzahlen

Woran wir Sicherheit, Bedienbarkeit und Leistung messen, gegliedert nach dem Qualitätsmodell **ISO/IEC 25010**.
**Blockierend** heißt: Die CI scheitert, wenn der Wert nicht erreicht wird. **Bericht** heißt: Der Wert wird bei jedem
Lauf gemessen und auf der Zusammenfassungsseite des CI-Laufs gezeigt (Job `performance`, Ergebnis in
`$GITHUB_STEP_SUMMARY`), hält die CI aber nie an.

## Sicherheit (Security)

| Kennzahl | Ziel | Referenz | Wo gemessen | Art |
|---|---|---|---|---|
| Anforderungen an die Anwendung | Stufe 2 | OWASP ASVS 5.0 | Reviews, Tests in `tests/` | Leitlinie |
| Häufigste Risiken abgedeckt | alle 10 | OWASP Top 10 (2025) | Reviews, CodeQL | Leitlinie |
| Funde der Code-Analyse | 0 offen | CodeQL (Default Setup) | Security → Code scanning | Bericht |
| Funde in den Workflows | 0 ab „medium“ | zizmor | CI-Job `workflows` | blockierend |
| Bekannte Lücken in Abhängigkeiten | 0 | pip-audit, Dependency Review | CI-Jobs `audit`, `dependency-review` | blockierend |
| Sicherheitsregeln im Code | 0 Funde | ruff `S` (Bandit) | CI-Job `test` | blockierend |
| Geheimnisse im Repository | 0 | gitleaks | CI-Job `secrets` | blockierend |
| Sicherheit des Repositorys | ≥ 7 von 10 | OpenSSF Scorecard | `scorecard.yml`, Security → Code scanning | Bericht |
| Sicherheit des Builds | Build L2 | SLSA | Build-Provenance in `release.yml` | erreicht |
| Härtung der systemd-Units | ≤ 2,0 | `systemd-analyze security` | `deploy/` | Leitlinie |
| Zeit bis zur Behebung einer Lücke | kritisch ≤ 7 Tage, hoch ≤ 30 Tage | übliche Fristen | Security Advisories, Dependabot | Leitlinie |

## Bedienbarkeit (UI)

| Kennzahl | Ziel | Referenz | Wo gemessen | Art |
|---|---|---|---|---|
| Barrierefreiheit | 0 Verstöße | WCAG 2.2 AA (EN 301 549, BFSG), axe-core | `tests/test_ui_browser.py`: jede Seite, hell und dunkel, Handy und Desktop | blockierend |
| Farbkontrast | ≥ 4,5 : 1 (Text) | WCAG 2.2 AA | axe-core im selben Test | blockierend |
| Keine Inline-Styles und -Skripte | 0 CSP-Verstöße | eigene CSP ohne `unsafe-inline` | Konsole im selben Test | blockierend |
| Handy zuerst | keine Seite breiter als 375 px | eigenes Ziel (#72) | selber Test | blockierend |
| Rückmeldung | sofort ≤ 0,1 s, ohne Unterbrechung ≤ 1 s, ab 10 s Fortschritt | Nielsen | Hintergrund-Jobs mit Fortschritt | Leitlinie |

axe findet automatisch etwa die Hälfte der WCAG-Probleme. Tastaturbedienung und Screenreader werden bei größeren
Änderungen an der Oberfläche von Hand geprüft.

## Leistung (Performance)

Gemessen in `tests/perf_service.py` auf dem CI-Runner, ohne echtes Netz. Die Werte schwanken je nach Runner;
entscheidend ist ihre Entwicklung von Lauf zu Lauf.

| Kennzahl | Ziel | Referenz | Art |
|---|---|---|---|
| TTFB (Antwortzeit des Servers), langsamste Seite | ≤ 800 ms | Core Web Vitals | Bericht |
| TTFB, Median aller Seiten | ≤ 200 ms | Nielsen 0,1 s | Bericht |
| LCP (größtes Element sichtbar), schlechteste Seite | ≤ 2,5 s | Core Web Vitals | Bericht |
| CLS (Layout springt), schlechteste Seite | ≤ 0,1 | Core Web Vitals | Bericht |
| INP (Reaktion auf Eingaben) | ≤ 200 ms | Core Web Vitals | entfällt: die Oberfläche hat kein JavaScript |
| Übertragene Daten, größte Seite | ≤ 500 KiB | Seitengewicht | Bericht |
| X-Ray mit großem Depot (15 ETFs, 13.500 Positionen): Antwortzeit (Median) | ≤ 800 ms | Core Web Vitals TTFB | Bericht |
| X-Ray mit großem Depot: Seitengewicht | ≤ 500 KiB | Seitengewicht | Bericht |
| Dauer eines Abrufs (300 Buchungen, 3000 vorhandene Aktivitäten) | ≤ 10 s | Nielsen, danach Fortschrittsanzeige | Bericht |
| Wealthfolio-Aufrufe je Buchung | ≤ 4 | eigenes Budget | Bericht |
| Lesende Wealthfolio-Aufrufe | kein Aufruf je Buchung (N+1) | eigenes Budget | Bericht |
| SQL-Anweisungen je Buchung | ≤ 10 | eigenes Budget | Bericht |
| Abfragen ohne Index | 0 | `EXPLAIN QUERY PLAN` | Bericht |
| Duplikat-Erkennung je Buchung bei 3000 vorhandenen (stündlich über ~4 Monate) | ≤ 10 ms | eigenes Budget | Bericht |

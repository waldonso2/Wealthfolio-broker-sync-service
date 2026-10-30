# Security audit 0.1.0a6 - secrets and web attack surface

Scope: SEC-06 (#152, secrets, encryption, sessions, logs, credential lifecycle) and SEC-07 (#153, web/API attack
surface) on `main` at 0.1.0a5 (14d2652). Method: reading the code, plus tests run against a local instance with
fabricated data (FastAPI `TestClient`, fake Wealthfolio, test-only broker). No third-party system was contacted.

Each item is **verified** (with the test that proves it), **fixed** (commit, with its regression test) or
**accepted** (why). "Not checked" means just that.

Fix commit: `859171d` (tests: `a992874`).

## 1. Secret flows (#152)

| Secret | Enters | At rest | Leaves the process | Deleted |
|---|---|---|---|---|
| UI password | `/setup-password`, `/login`, reconfirm fields | argon2id hash in `secrets.enc` | never | replaced on change; `reset-ui-password` removes hash and session key |
| Master passphrase | `/security/passphrase`, `/unlock` | never; argon2id result only in `/run/.../unlock.key` (0600) or process memory | never | lock / restart |
| Host key | systemd credential, `COMPANION_KEY_FILE`, `data/secret.key` (dev only) | outside `data/` in production | never | rotation (`rotate-key`) |
| Broker credentials | broker form (`cred_*`) | `secrets.enc` `brokers.<key>.credentials` | to the broker by its adapter | **new**: *Zugangsdaten löschen*; saving new ones replaces them |
| `ask=True` PIN | login challenge | memory only (`AppContext.pins`, `Syncer._pins`) | to the bank | popped after the run |
| Broker sessions/tokens | adapter after login | `secrets.enc` `brokers.<key>.session` | to the broker | new credentials, safe mode, **new** deletion |
| Wealthfolio password | `/setup/wealthfolio` | `secrets.enc` | to the saved Wealthfolio URL only (**fixed**) | "kein Passwort" |
| logo.dev key | `/services/logos/settings` | `secrets.enc` | to `*.logo.dev` over https only (**fixed**) | form "forget" |
| ntfy token | `/notifications` | `notify.enc` (host tier) | to the saved ntfy server; **fixed**: dropped when the server changes | "forget token" |
| Metrics token | env `COMPANION_METRICS_TOKEN` | not stored | compared with `compare_digest` | env |
| Setup code | console only | SHA-256 + expiry in `secrets.enc` | never logged, never in a redirect | used up on success; expires after 24 h |
| UI session key | generated once | `secrets.enc` `ui_session_key` | signs the cookie | `reset-ui-password` |

### Verified

- **argon2id parameters**: UI password and passphrase use t=3, m=64 MiB, p=4 (RFC 9106 second recommendation);
  scrypt hashes of before 0.9.0 are upgraded at login. Tests: `test_secret_flows::test_the_real_argon2id_cost`
  (new - the suite lowers the cost, so nothing checked the shipped values before), `test_security::test_old_scrypt_hashes_still_work_and_are_upgraded`.
- **AES-256-GCM**: a fresh random 96-bit nonce per seal, AAD = magic + purpose, a subkey per purpose (HKDF);
  a changed tag/ciphertext/nonce byte, another purpose, another key, a truncated or plain file all raise.
  A vault under another host key raises instead of returning empty data a save would overwrite.
  Tests: `test_secret_flows::test_encryption_fails_closed`, `::test_the_host_and_data_keys_are_separate`,
  `test_security::test_a_file_cannot_be_swapped_for_another`.
  Random nonces are safe for this volume (files written thousands, not billions, of times per key).
- **Key derivation/separation**: host tier = HKDF(host key, "brokersync host v1"); data tier = HKDF(host key ‖
  argon2id(passphrase)); host and data keys differ, and so do per-purpose subkeys (test above).
- **Missing key**: `KeyMissing` once data exists, never a new key over existing data
  (`test_security::test_the_key_comes_from_systemd_and_is_required_once_data_exists`); a stale/planted unlock cache
  counts as locked (existing check value).
- **Nothing secret returned by a route**: every GET page of every router (hidden pages too), the broker form after an
  error with a typed secret, and the Wealthfolio test form contain none of the planted secrets - not in the body,
  the headers or the (signed, not encrypted) session cookie. Test: `test_secret_flows::test_no_page_header_or_cookie_carries_a_stored_secret`.
  The session cookie holds only `user`, a 16-hex tag, timestamps, the CSRF token and redacted flash texts.
- **Redaction**: vault strings and the typed PIN are registered; the filter rewrites tracebacks (`exc_text`) on every
  root handler, plain and JSON. Tests: `test_secret_flows::test_tracebacks_go_through_the_redaction` (new),
  `test_security::test_the_log_never_shows_secrets_or_account_numbers`, `::test_json_logs_are_redacted_like_the_plain_ones`.
  uvicorn runs with `log_config=None`, so its records reach the root handlers and their filter; the access log is off.
- **HTTP errors**: an unexpected exception shows only an id (`test_web::test_an_unexpected_error_shows_only_an_id`);
  stored and shown foreign error texts are masked (`test_security::test_error_texts_are_stored_and_shown_masked`).
- **ntfy**: messages pass through `redact`; details only on request (`test_security::test_notifications_leave_out_details_unless_asked`).
- **Backups**: the archive holds only sealed files and the SQLCipher database, never `secret.key`; no planted secret
  readable after gunzip. Restore checks everything opens before touching `data/`, extracts only regular files
  `data/<name>` with `O_EXCL`, 0600, under the UI lock. Tests: `test_secret_flows::test_a_backup_holds_no_secret_in_plain_text` (new), `test_backup.py`.
- **Reset password path**: `companion-reset-password` → `companion-cli reset-ui-password` removes the hash and the
  session key, prints a new setup code on the console only. Every old cookie fails the session tag check
  (`test_security::test_a_cookie_of_an_earlier_password_signs_in_nobody`).
- **`ask=True` PIN**: never in the vault or a session blob (`test_security::test_the_fints_session_blob_never_holds_the_pin`).
- **Sessions**: see 2.

### Fixed

- **No way to delete a broker's credentials** (real): a secret field left empty keeps the stored value, and nothing
  else removed `brokers.<key>` from the vault - comdirect's client secret, N26's refresh token or Trade Republic's
  cookies stayed until a reinstall. New `POST /brokers/{key}/forget` (*Zugangsdaten löschen* on the broker page):
  password again after 15 min, removes credentials and session, drops a pending login and a typed PIN, switches the
  broker's fetch off, logs the event. Tests: `test_attack_surface::test_credentials_can_be_deleted_with_the_password`,
  `::test_deleting_credentials_asks_for_the_password_after_a_while`. It does not log out at the broker (no adapter
  call); the bank-side session expires on its own.
- **ntfy token not masked in the log** (hardening; no leaking log line was found): it lives in `notify.enc`, not the
  vault, so `redact` never knew it. `config._read` registers it now.
  Test: `test_secret_flows::test_the_ntfy_token_is_masked_although_it_lives_outside_the_vault`.
- **Damaged unlock cache** (robustness): a non-hex `/run/.../unlock.key` raised `ValueError` on every request; it
  now counts as locked. Test: `test_attack_surface::test_a_corrupt_unlock_cache_locks_instead_of_failing`.

### Accepted

- **Trade Republic's 4-digit PIN is not masked by the safety net** (`redact.MIN_SECRET` = 6: a 4-digit number would
  mask ordinary text). The code never logs it; the net catches only accidents.
- **The session cookie is signed, not encrypted** - it carries nothing secret (verified above).
- **Restore reads the archive into memory** - root-only CLI, member size capped.

## 2. Web/API attack surface (#153)

### Routes

All routes are GET or POST (`test_attack_surface::test_only_get_and_post_exist`). Public without a login - exactly:
`GET/POST /login`, `GET /healthz` (version only after login), `GET /ca.crt` (public certificate),
`GET/POST /setup-password` (POST needs the CSRF token and the one-time code), and `/static/*`. `/metrics` also opens
with the bearer token. Every other route (66 definitions incl. the hidden *Duplikate*; 86 requests once the service keys are filled in) needs a session and redirects to `/login`
without one; every POST but `/login` needs the CSRF token. State-changing GETs: `GET /notifications` saves a new
random topic once, `GET /check` and `GET /lookthrough/fire` start a read-only background read - all behind the login
and the `SameSite=Strict` cookie.

Tests (table-driven over `ROUTERS + HIDDEN`, so a new route is covered as soon as it exists):
`test_only_the_allow_list_opens_without_a_login` (the login check's exemptions must equal the allow-list),
`test_every_route_but_the_allow_list_needs_a_login` (also a forged cookie),
`test_every_post_needs_the_csrf_token_and_changes_nothing_without_it` (missing and forged token; `data/` byte-identical
afterwards). The locked app answers only `/healthz`, `/unlock` and redirects.

### Verified

- **Session fixation**: login clears the session; the pre-login CSRF token is dead afterwards
  (`test_a_login_starts_a_new_session`).
- **Cookie**: `HttpOnly`, `Secure` (HTTPS), `SameSite=Strict`, `Path=/`, `Max-Age=43200` (`test_session_cookie_flags`);
  idle 30 min / absolute 12 h, logout, password change and "end other sessions" invalidate copied cookies
  (`test_ui_sessions.py`, `test_security::test_a_cookie_of_an_earlier_password_signs_in_nobody`).
- **XSS**: Jinja autoescape on, no `|safe` or `autoescape false` in any template, `Markup.format` escapes the params of
  our own HTML texts (`test_templates_escape_everything`); the unexpected-error page escapes by hand; unknown payloads
  render via `tojson` in `<pre>`. CSP `script-src 'none'`, no inline styles (`test_security::test_no_inline_styles_and_the_health_check_says_little`).
- **Headers** on every answer, also redirects, errors and static files (`test_headers_on_every_answer_also_errors_and_redirects`).
- **Host header / DNS rebinding**: only IPs and known names (`test_security::test_only_known_names_reach_the_ui_and_the_redirect`).
- **Request size**: `Content-Length` over 6 MB refused before reading, chunked bodies cut
  (`test_inputs::test_oversized_requests_are_refused_before_reading`, `::test_a_chunked_body_is_cut_off`);
  uploads read at most 5 MB + 1, parsed in memory, no file path from the client.
- **Brake bypasses**: `X-Forwarded-For` is ignored without `COMPANION_TRUST_PROXY`
  (`test_a_forwarded_header_does_not_get_past_the_brake`); every password, passphrase and setup-code check in
  `web/` sits in `Throttle.attempt` (`test_every_password_check_of_the_ui_goes_through_the_brake`); IPv6 /64,
  global limit, parallel slots (`test_security::test_the_brake_*`).
- **Open redirects**: `/theme` and the error page's back link accept only a path of this site
  (`test_views` theme test, `test_web::test_the_error_pages_back_link_stays_on_this_site`).
- **Outbound URL policy**: Wealthfolio client never follows redirects and pins a certificate fingerprint when set;
  look-through sources only `HOSTS` over https, redirects included; logo.dev only `*.logo.dev` over https (fixed);
  comdirect's status link gets its token only on `api.comdirect.de`; ntfy follows no redirects (httpx default).
- **Deserialization/XML**: no pickle/yaml/eval; XLSX/XML through ElementTree on expat 2.6 (no external entities,
  entity-expansion protection), zip size checked.
- **Debug endpoints**: `docs_url`, `redoc_url`, `openapi_url` off in both apps.

### Fixed

- **Cross-site lock-out** (real, low): `/login` and `/unlock` take no CSRF token, so any website the owner visits
  could post wrong passwords to the UI's address through the browser and spend the brake (5 per client, 30 in all)
  - locking the owner out for 15 minutes. Forms with `Sec-Fetch-Site: cross-site` are now refused (403) before any
  check, on every POST. Tests: `test_a_form_from_another_site_is_refused_before_any_password_check`,
  `test_the_unlock_page_refuses_another_site_too`. Browsers without `Sec-Fetch-*` (very old ones) are not covered.
- **Credential forwarding to a new Wealthfolio address** (real, medium): `/setup/wealthfolio` used the *stored*
  password for whatever URL was entered, also for *Nur testen* - a hijacked or unattended session could send it to
  any host. A changed URL now needs the password typed again; nothing is sent before.
  Test: `test_the_stored_wealthfolio_password_never_goes_to_a_new_address`.
- **ntfy token forwarded to a new server** (real, low): same pattern; the token is dropped when the server changes
  unless a new one is typed. Test: `test_the_ntfy_token_stays_with_its_server`.
- **logo.dev redirect to http** (hardening): the redirect check looked at the host only; the key travels in the URL.
  Test: `test_logo_dev_redirects_only_over_https`.

### Accepted

- **Configurable URLs reach the LAN by design**: the Wealthfolio URL, ntfy server and a FinTS server are chosen by the
  logged-in owner and usually private; they are not restricted to public addresses. Plain `http://` Wealthfolio is
  allowed with a warning on the page.
- **The brake is in memory**: a restart resets it; restarting needs host access.
- **A changed Wealthfolio fingerprint keeps the stored password**: redirecting traffic to another host still needs
  a network position in addition to a session.
- **Throttle lock-out from the LAN**: any host in the network can still spend the attempts of the global limit; that
  is the price of a global limit against distributed guessing.

### Not checked

- python-fints' (requests') redirect handling towards a FinTS server: a 307/308 would repeat the POST with the PIN.
  The server is the bank's from the DK list or an https address the owner typed.
- Each adapter's exception texts one by one; they are masked where stored and shown, and in the log.
- `deploy/` scripts beyond the reset-password helper (another work stream).

# Secrets & Test Isolation: lessons learnt

**Scope.** Secrets in templates, tests reaching production, test-data flags.

| id | severity | lesson |
|---|---|---|
| SE-001 | critical | `.env.example` created by copying the live `.env` |
| SE-002 | critical | The test suite reaching production |
| SE-003 | medium | Test-data flags live on rows; nothing flags a FILE or an unflagged row |
| SE-004 | high | Never probe a live mutation endpoint with real data |
| SE-005 | critical | A static build publishes the server's own source, credentials included |

## SE-001 — `.env.example` created by copying the live `.env`

*Severity:* **critical** · *Stacks:* secrets

**Symptom.** 51 of 58 production secrets were committed and pushed, and nine permission rules held database credentials in plaintext.

**Root cause.** A template was made by copying the real file.

**Resolution.** Every value in an example file is a placeholder. Rotate everything that was committed, and remember that removing it from the tree does not clear history. An encryption key for data at rest needs re-encryption, not just a value swap.

**Prevention.** Run a secret scanner (gitleaks/trufflehog) in pre-commit and CI.

**How it is checked.**

- `shell`: `gitleaks detect --redact`
- `regex`: `(?i)(secret|password|api_key|token)\s*=\s*(?!<)[A-Za-z0-9/+_\-]{12,}  -- in *.example`

**Evidence.** CLAUDE.md §Secrets (2026-08-26)

## SE-002 — The test suite reaching production

*Severity:* **critical** · *Stacks:* testing, secrets

**Symptom.** Test users (1,772 active super admins with a committed password) leaked into production. A module-level `load_dotenv(override=True)` executed at COLLECTION redirected the whole suite to production. Tests relayed live mail despite a kill switch, because the mocked switch fell through to real smtplib. Tests wrote fixtures into a production comparison table.

**Root cause.** Test and production shared credentials, and mocks covered some stores but not others.

**Resolution.** A separate test database with separate credentials that production refuses; an autouse fixture asserting the DSN is a test DSN; no `override=True` in importable modules; an `under_pytest()` backstop that flags writes as test data; a sweep of every flagged table; and 'mock EVERY store a handler writes to'.

**Prevention.** An auth gate refuses test-data accounts in production.

**How it is checked.**

- `regex`: `load_dotenv\([^)]*override=True`
- `test`: session fixture asserts DB name/host is the test one

**Evidence.** 8ea663861; 5145b880b; memory: pattern_one_import_redirected_the_whole_suite_to_production; memory: pattern_test_suite_relayed_live_mail_via_mocked_gate

## SE-003 — Test-data flags live on rows; nothing flags a FILE or an unflagged row

*Severity:* **medium** · *Stacks:* testing

**Symptom.** A fake PDF was left in real storage. Unflagged test rows became live credentials.

**Root cause.** The flag exists only where somebody set it.

**Resolution.** An autouse fixture redirects file storage, and code under test ORs in `under_pytest()`. An orphan-file sweep refuses to run if any claim collection fails to read.

**Prevention.** Never let the CALLER set the flag (a header or body field can hide audit rows).

**How it is checked.**

- `regex`: `is_test_data\s*[:=]\s*(request|body|payload|headers)`

**Evidence.** memory: pattern_bytes_on_disk_have_no_row_to_flag; memory: pattern_caller_settable_test_flag_suppresses_audit

## SE-004 — Never probe a live mutation endpoint with real data

*Severity:* **high** · *Stacks:* operations, security

**Symptom.** A diagnostic registration took over two real accounts, and one real name was overwritten.

**Root cause.** An 'existing email' branch claimed the account instead of returning 409.

**Resolution.** Read the branch logic first, or probe with an address that can't belong to anyone (`probe+x@example.invalid`).

**Prevention.** Use RFC 2606 reserved domains for all probes.

**How it is checked.**

- `review`: curl/probe commands against prod with real identifiers

**Evidence.** footgun #12

## SE-005 — A static build publishes the server's own source, credentials included

*Severity:* **critical** · *Stacks:* secrets, deploy, javascript, nginx

**Symptom.** `curl https://site/contact/feedback_server.py` returned 200 with `ADMIN_PASSWORD = '...'` and the Flask `SECRET_KEY` in plain text. The PHP admin panel's source and a shell script that echoed the same password were served the same way.

**Root cause.** The bundler's copy step listed a directory of operator scripts into the output directory the web server serves as static files. Python and PHP are not executed there, so the request falls through to the static handler and returns the file verbatim. The admin panel had therefore never worked — getting its source back over HTTP is the proof.

**Resolution.** Drop them from the copy patterns and run operator scripts outside the document root. Rotate every credential the file carried: deleting the file does not un-publish what was already fetched, and the value survives in git history. Read the access log to see who fetched those paths.

**Prevention.** Assert the build output contains no server-side source, and deny those suffixes at the web server as defence in depth.

**How it is checked.**

- `regex`: `from:\s*['\"][^'\"]*\.(py|php|sh|env|ini|bak)['\"]`
- `shell`: `find dist -regex '.*\.\(py\|php\|sh\|env\|ini\|bak\)$'`
- `ci`: fetch each server-source path from the deployed site and require 404

**Evidence.** retirement_calculator_au (2026-09-20 audit): four files served 200 from /contact/ before the copy patterns were removed

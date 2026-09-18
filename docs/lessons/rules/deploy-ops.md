# Deploy & Operations: lessons learnt

**Scope.** Schedulers, systemd, preflights, live builds.

| id | severity | lesson |
|---|---|---|
| OP-001 | high | One job run by two schedulers |
| OP-002 | medium | Units present in the repo but not installed on the host |
| OP-003 | medium | systemd units run with a minimal PATH and environment |
| OP-004 | high | A hardcoded version used as an oracle ('is this ours?') |
| OP-005 | medium | Preflight ordering and interactive sudo mid-deploy |
| OP-006 | medium | An install script that provisions only part of what the application needs |
| OP-007 | high | Never rebuild or delete `.next` in the directory production is serving from |

## OP-001 — One job run by two schedulers

*Severity:* **high** · *Stacks:* operations, cron, systemd

**Symptom.** Nine jobs ran from BOTH crontab and a systemd timer. A crontab-only deduplication pass could not see the other scheduler.

**Root cause.** No single source of truth for scheduling.

**Resolution.** Pick ONE scheduler per job, and list each job's scheduler in one manifest.

**Prevention.** Audit `crontab -l` and `systemctl list-timers` together.

**How it is checked.**

- `shell`: `crontab -l; systemctl list-timers --all  -- intersect job names`

**Evidence.** cd25e84b9; f7805a246; memory: pattern_two_schedulers_one_job

## OP-002 — Units present in the repo but not installed on the host

*Severity:* **medium** · *Stacks:* operations, systemd

**Symptom.** Cron scripts that 'exist' never ran. Ten units in the repo were not installed.

**Root cause.** Nothing reconciled the repo with the host.

**Resolution.** The installer installs every unit, and a preflight diffs `deploy/systemd/*` against `systemctl list-unit-files`.

**Prevention.** Don't assume a script ran because it exists.

**How it is checked.**

- `shell`: `comm -23 <(ls deploy/systemd) <(systemctl list-unit-files --no-legend | awk '{print $1}')`

**Evidence.** 0f190a2d5; d6602e03a

## OP-003 — systemd units run with a minimal PATH and environment

*Severity:* **medium** · *Stacks:* systemd

**Symptom.** The command worked in a shell and failed under the unit with 'not found'.

**Root cause.** Units don't read shell profiles.

**Resolution.** Use absolute paths (the venv's python), and `Environment=`/`EnvironmentFile=`.

**Prevention.** Test with `systemd-run --user --wait` or `env -i`.

**How it is checked.**

- `regex`: `^ExecStart=(?!/)`

**Evidence.** 759c87434

## OP-004 — A hardcoded version used as an oracle ('is this ours?')

*Severity:* **high** · *Stacks:* deploy, shell

**Symptom.** A macOS deploy told the operator to move their own PostgreSQL 18 cluster off its port. A Linux preflight named a major version nothing identified as ours.

**Root cause.** It inferred ownership from a version literal, not from identity (data directory, port, owning role).

**Resolution.** Identify ownership by what you configured (DSN host, port and data directory), never by version. pg_createcluster is Debian-only, and checksums are a per-cluster setting.

**Prevention.** Grep scripts for version literals in conditionals.

**How it is checked.**

- `regex`: `if .*(==|-eq)\s*['\"]?1[4-9]['\"]?  -- version literal in a deploy conditional`

**Evidence.** 985fda5a2; b3ad4be56; 5fb847ef8; e082c0b32; 1157c2508

## OP-005 — Preflight ordering and interactive sudo mid-deploy

*Severity:* **medium** · *Stacks:* deploy

**Symptom.** A deploy half-ran, then stopped at a sudo prompt or a check that should have run first.

**Root cause.** Cheap checks ran after expensive mutations.

**Resolution.** Order the deploy as read-only checks → build → migrate → restart. Request sudo up front (`sudo -v`) and stop and prompt the operator rather than storing credentials.

**Prevention.** A preflight must be side-effect free.

**How it is checked.**

- `review`: mutations before all read-only checks in deploy scripts

**Evidence.** a903afef2; 80429fd94; fbc906b5c

## OP-006 — An install script that provisions only part of what the application needs

*Severity:* **medium** · *Stacks:* deploy

**Symptom.** A provisioned machine had no Playwright (it lives in the root package.json), and no DB role ownership.

**Root cause.** The installer covered only one package manager or directory.

**Resolution.** The installer installs every manifest, and a smoke test runs each toolchain's `--version`/`--list`.

**Prevention.** Provision from scratch in CI occasionally.

**How it is checked.**

- `ci`: fresh-container install + smoke

## OP-007 — Never rebuild or delete `.next` in the directory production is serving from

*Severity:* **high** · *Stacks:* nextjs, deploy

**Symptom.** A mid-deploy build and `rm -rf .next` killed the operator's own build twice, and the live site errored.

**Root cause.** The running server reads `.next` lazily.

**Resolution.** Build into a separate directory or release folder and switch with a symlink, or build while stopped.

**Prevention.** Never run `next build` in the live tree during a session.

**How it is checked.**

- `review`: scripts running next build/rm .next in the served dir

**Evidence.** memory: pattern_yarn1_upgrade_is_a_noop_for_transitive_deps

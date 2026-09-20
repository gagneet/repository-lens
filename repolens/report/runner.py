"""`repolens report`: run every tool, and produce ONE prioritised findings list.

    repolens report                         the default tool set -> .repolens/report/
    repolens report --only security,performance
    repolens report --with impact,osv-scanner   add the slow or networked tools
    repolens report --update-baseline       accept today's findings as known
    repolens report --check --fail-on P1    CI: fail on a NEW finding at P1 or worse

Writes `report.md` (to read), `report.html` (a self-contained page with no scripts),
`report.json` (to query) and `report.sarif` (for GitHub code scanning or any SARIF viewer).

A tool that cannot run is SKIPPED with the reason, and a tool that crashes is an ERROR;
neither is ever reported as zero findings, because a silent zero and a clean result read
identically. An external tool's exit code is checked and its JSON must parse — an empty
stdout from a crashed scanner is an error, never `{}` — and a file the tool reports it
could NOT scan is itself a finding. `--check` fails on an errored tool for the same
reason: a report that could not look cannot vouch. A SKIPPED tool fails `--check` only if
it is listed in `[report] require`, since what is installed differs between machines.
A tool named with `--require` is an assertion by this invocation, so its being skipped,
erroring or not selected exits 1 in EVERY mode, and refuses `--update-baseline` (exit 2).

Only medium- and high-confidence findings can fail `--check`. Low-confidence ones are
review candidates (a "hotspot", in SonarQube's terms): a gate that fails on guesses is a
gate that gets allowlisted into uselessness.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .. import __version__
from ..core.html_report import render_html
from ..config import Config, load_config, merge
from ..core.findings import (PRIORITIES, BaselineError, Finding, ToolRun, load_baseline,
                             load_magnitudes, mark_new, sort_key, summary, to_json, to_markdown,
                             to_sarif, write_baseline)
from ..core.git import is_modified, run_git
from ..provenance import describe_build, tool_build

DEFAULTS: dict[str, Any] = {
    "title": "repolens report",
    "out_dir": ".repolens/report",
    "baseline": ".repolens/report_baseline.json",
    "tools": ["security", "performance", "migrations", "featuretrace", "owners", "gates", "artefacts",
              "docstrings", "commands", "sarif-commands", "ruff", "bandit",
              # semgrep is in the defaults although it is optional: its adapter REFUSES a
              # registry config, so it can only ever run against local rules, and until
              # `semgrep_config` is set it reports itself SKIPPED with the reason. A tool
              # that is silently not in the list looks the same as one that found nothing.
              # osv-scanner and pip-audit stay out because they query api.osv.dev and PyPI,
              # and a default report must not reach the network.
              "semgrep"],
    "fail_on": "P1",
    # Tools whose being SKIPPED fails --check (say, ruff and bandit on a CI image that
    # installs them). Empty by default: a laptop without bandit still gets a report.
    "require": [],
    # Interpreter for `{python}` in commands, when it exists.
    "venv_python": "",
    "command_timeout_seconds": 300,
    # [{name, run = [argv], severity, category, remedy}] — a non-zero exit is one finding.
    "commands": [],
    # [{name, run = [argv] containing {output}, ok_exit = [...]}] — any analyser that writes
    # SARIF joins the report per finding instead of as one pass/fail. `{output}` is replaced
    # with a temporary path the tool writes to. This is the seam for trivy, gitleaks, grype,
    # checkov, hadolint and anything else that speaks SARIF; each keeps its own ToolRun, so a
    # missing binary is SKIPPED and a crash is ERROR, never a silent zero findings.
    "sarif_commands": [],
    "ruff_select": ["S", "ASYNC", "B"],
    # B008 flags a call in an argument default — which is FastAPI's documented
    # `Depends()` idiom, so on a FastAPI codebase it is two thousand false positives.
    "ruff_ignore": ["B008"],
    "semgrep_config": "",
    "requirements": [],
    # fingerprint -> the reason it is accepted. Reviewed, not ignored: it stays visible here.
    "suppress": {},
}
#: Not in the default list: `impact` re-scans the whole graph, and osv-scanner/pip-audit
#: query api.osv.dev and PyPI. Enable those deliberately with `--with`, knowing they leave
#: the machine. `semgrep` is optional to CONFIGURE but is in the default list, because it
#: is offline by construction here and says so when it is not configured.
OPTIONAL_TOOLS = ("impact", "osv-scanner", "pip-audit")
#: Selected by name like an adapter, but configured rather than implemented: each expands
#: to zero or more runs from `[report] commands` / `[report] sarif_commands`.
_PSEUDO_TOOLS = ("commands", "sarif-commands")


class Skip(Exception):
    """The tool cannot run here; the message says why and how to enable it."""


@dataclass
class Context:
    cfg: Config
    section: dict[str, Any]
    _scan: Any = field(default=None, repr=False, compare=False)

    def scan_settings(self) -> Any:
        """`[scan]` settings, made once per report: security, performance and migrations
        then share one parse cache (`ScanSettings.parse_cache`) instead of each parsing the
        whole tree again."""
        if self._scan is None:
            from ..scan.settings import from_config
            self._scan = from_config(self.cfg)
        return self._scan

    @property
    def root(self) -> Path:
        return self.cfg.root

    def python(self) -> str:
        venv = self.section["venv_python"]
        if venv and (self.root / venv).exists():
            return str(self.root / venv)
        return sys.executable

    def executable(self, name: str) -> str:
        """`name` in $REPOLENS_TOOLS_BIN, beside the configured venv interpreter, beside the
        interpreter running repolens, or on PATH, in that order.

        The running interpreter's own bin directory matters: `python -m pip install ruff`
        into an unactivated venv puts ruff there and nowhere on PATH, and the report then
        called an installed tool "not installed"."""
        extra = [os.environ.get("REPOLENS_TOOLS_BIN", "")]
        if self.section["venv_python"]:
            extra.append(str((self.root / self.section["venv_python"]).parent))
        # Not resolved: a venv's python is a symlink out of the venv, and its tools are not.
        running = Path(sys.executable).parent
        extra.append(str(running))
        if os.name == "nt" and running.name.lower() != "scripts":
            extra.append(str(running / "Scripts"))  # a base Windows install keeps tools there
        search = os.pathsep.join([p for p in extra if p] + [os.environ.get("PATH", "")])
        found = shutil.which(name, path=search)
        if not found:
            raise Skip(f"{name} is not installed (pip install {name}, or set REPOLENS_TOOLS_BIN)")
        return found

    def python_roots(self) -> list[str]:
        """The configured `[scan] python_roots` that exist. Skips the calling tool when
        there are none: `ruff check` with no path scans the whole checkout, and `bandit -r`
        with none is an error — neither is what an empty list meant."""
        roots = merge({"python_roots": ["."]}, self.cfg.section("scan"))["python_roots"]
        found = [r for r in roots if (self.root / r).exists()]
        if not found:
            raise Skip("no [scan] python_roots exist in this repository")
        return found


def _rel(ctx: Context, path: str) -> str:
    p = Path(path)
    try:
        return (p if p.is_absolute() else (ctx.root / p)).resolve().relative_to(ctx.root).as_posix()
    except ValueError:
        return p.as_posix()


# ── built-in tools ───────────────────────────────────────────────────────────────
def _security(ctx: Context) -> list[Finding]:
    from ..scan import security
    return security.scan(ctx.scan_settings())


def _performance(ctx: Context) -> list[Finding]:
    from ..scan import performance
    return performance.scan(ctx.scan_settings())


def _migrations(ctx: Context) -> list[Finding]:
    from ..scan import migrations
    return migrations.scan(ctx.scan_settings())


_FT_REMEDY = {
    "featuretrace/references-a-path-that-does-not-exist": "Fix or remove the stale path in the marker block.",
    "featuretrace/missing-data-flow": "Add a `Data flow:` line: where the data comes from and goes.",
    "featuretrace/missing-related": "Add `Related:` naming the other files in the chain.",
    "featuretrace/missing-scope-qualifier": "State the scope qualifier the repository configures (`[featuretrace] scope_values`), e.g. (tenant-scoped) or (global).",
    "featuretrace/missing-layer-needed-for-map-generation": "Add `Layer:` so the maps can place the file.",
    "featuretrace/marker-not-near-top": "Move the marker into the first lines of the file.",
}


def _featuretrace(ctx: Context) -> list[Finding]:
    from ..core.files import read_text_or_none, rel
    from ..featuretrace.audit import AuditContext, markers_for
    from ..featuretrace.settings import from_config

    audit = AuditContext(from_config(ctx.cfg))
    out: list[Finding] = []
    for path in audit.iter_files():
        text = read_text_or_none(path)
        if text is None:
            continue
        for marker in markers_for(audit, path, text):
            for issue in marker.issues:
                kind = re.sub(r"[^a-z0-9]+", "-", issue.split(":")[0].lower()).strip("-")
                rule = f"featuretrace/{kind}"
                out.append(Finding(
                    tool="featuretrace", rule=rule, confidence="high", category="traceability",
                    severity="medium" if kind.startswith("references-a-path") else "low",
                    file=rel(path, audit.root), line=marker.line_no,
                    message=f"[{marker.tag}] {issue}", remedy=_FT_REMEDY.get(rule, ""),
                ))
    return out


def _owners(ctx: Context) -> list[Finding]:
    from ..owners.registry import OwnerRegistry
    from ..owners.settings import from_config

    s = from_config(ctx.cfg)
    if not s.registry.exists():
        raise Skip(f"no capability registry at {s.rel(s.registry)}")
    reg = OwnerRegistry(s)
    try:
        data = reg.load_registry()
    except SystemExit as exc:  # PyYAML missing
        raise Skip(str(exc)) from None
    out = [Finding(tool="owners", rule="owners/registry-schema", severity="medium", confidence="high",
                   category="duplication", file=s.rel(s.registry), message=problem)
           for problem in reg.validate_schema(data)]
    report = reg.evaluate(data)
    for c in report["concepts"]:
        lines = {h["key"]: h["line"] for h in c["detail"]}
        concept, owner = c["concept"], c["owner"]

        def at(key: str) -> tuple[str, int]:
            return key.rsplit(":", 1)[0], lines.get(key, 0)

        if not c["owner_exists"]:
            out.append(Finding(tool="owners", rule="owners/owner-missing", severity="high",
                               confidence="high", category="duplication", file=owner,
                               message=f"[{concept}] the registered owner module does not exist"))
        for symbol in c["missing_symbols"]:
            out.append(Finding(tool="owners", rule="owners/owner-symbol-removed", severity="high",
                               confidence="high", category="duplication", file=owner,
                               message=f"[{concept}] {owner} no longer defines {symbol!r}",
                               remedy="Restore the canonical entry point, or re-point the registry "
                                      "at its replacement — callers roll their own the moment it goes."))
        for test in c["tests"]:
            if not (s.root / test).exists():
                out.append(Finding(tool="owners", rule="owners/enforcement-test-missing",
                                   severity="medium", confidence="high", category="duplication",
                                   file=s.rel(s.registry),
                                   message=f"[{concept}] names a test that does not exist: {test}"))
        for key in c["new_violations"]:
            file, line = at(key)
            out.append(Finding(tool="owners", rule="owners/second-implementation", severity="high",
                               confidence="high", category="duplication", file=file, line=line,
                               message=f"[{concept}] {key} re-implements a concept owned by {owner}",
                               remedy=f"Call {owner}. Rule: {c['rule'][:200]}"))
        for key in c["resolved_violations"]:
            file, line = at(key)
            out.append(Finding(tool="owners", rule="owners/stale-known-violation", severity="low",
                               confidence="high", category="duplication", file=file, line=line,
                               message=f"[{concept}] {key} is fixed but still in known_violations",
                               remedy="Remove it from known_violations so the ratchet holds."))
        for key in c["known_violations"]:
            if key in c["resolved_violations"]:
                continue
            file, line = at(key)
            out.append(Finding(tool="owners", rule="owners/recorded-debt", severity="medium",
                               confidence="high", category="duplication", file=file, line=line,
                               message=f"[{concept}] {key} is recorded debt: a second implementation of a concept owned by {owner}",
                               remedy=f"Replace it with a call to {owner}, then drop it from known_violations."))
    return out


def _gates(ctx: Context) -> list[Finding]:
    from ..gates import reachability

    g = reachability.from_config(ctx.cfg)
    if not g.scripts:
        raise Skip("no [gates] scripts configured")
    found = reachability.evaluate(g)
    # The evaluation speaks in file NAMES; a finding needs the repo-relative path, or its
    # SARIF location points at a file in the repository root that does not exist.
    where = {p.name: p.relative_to(g.root).as_posix() for p in [*g.scripts, *g.must_fail]}
    out = [Finding(tool="gates", rule="gates/unreachable", severity="medium", confidence="high",
                   category="hygiene", file=where.get(name, name),
                   message=f"{name} is executed by nothing",
                   remedy="Wire it into CI/deploy/a test, or exempt it with a reason.")
           for name in found.unreachable]
    out += [Finding(tool="gates", rule="gates/cannot-fail", severity="medium", confidence="high",
                    category="hygiene", file=where.get(name, name),
                    message=f"{name} can never exit non-zero",
                    remedy="Give it a --check mode that fails on a finding.")
            for name in found.non_gating]
    out += [Finding(tool="gates", rule="gates/stale-exemption", severity="low", confidence="high",
                    category="hygiene", message=line, remedy="Remove the exemption.")
            for line in found.stale_exemptions]
    return out


def _artefacts(ctx: Context) -> list[Finding]:
    from ..artefacts.settings import from_config
    from ..artefacts.verify import disagreements

    s = from_config(ctx.cfg)
    if not s.rules:
        raise Skip("no [artefacts] rules configured")
    unowned, unrouted = disagreements(s)
    out = [Finding(tool="artefacts", rule="artefacts/routed-but-not-regenerated", severity="medium",
                   confidence="high", category="hygiene", file=p,
                   message="merge=generated keeps one side on a conflict, and no rule regenerates it",
                   remedy="Add a regeneration rule, or exempt it from the driver in .gitattributes.")
           for p in unowned]
    out += [Finding(tool="artefacts", rule="artefacts/regenerated-but-text-merged", severity="medium",
                    confidence="high", category="hygiene", file=p,
                    message="a rule regenerates it, but .gitattributes lets git text-merge it",
                    remedy="Route it to the merge driver in .gitattributes.")
            for p in unrouted]
    return out


def _docstrings(ctx: Context) -> list[Finding]:
    """A file that gained an undocumented public symbol against the docstring baseline.
    The ratchet itself is `repolens docs coverage --check`; this puts its regressions in
    the one list, where a reader looks."""
    from ..docs.coverage import measure, missing_counts, ratchet_for
    from ..docs.settings import from_config

    s = from_config(ctx.cfg)
    ratchet = ratchet_for(s)
    baseline = ratchet.load()
    if baseline is None:
        raise Skip(f"no docstring baseline at {s.baseline.relative_to(s.root).as_posix()} "
                   f"(`{s.command} --update-baseline`)")
    results = {r.path: r for r in measure(s)}
    out = []
    for path, live in sorted(missing_counts(list(results.values()), baseline).items()):
        allowed = baseline.get(path, 0)
        if live <= allowed:
            continue
        missing = results[path].missing
        out.append(Finding(
            tool="docstrings", rule="docs/undocumented-public-symbol", severity="low",
            confidence="high", category="documentation", file=path,
            line=missing[0].line if missing else 0,
            message=(f"{live} undocumented public symbol(s) where the baseline allows {allowed}: "
                     + ", ".join(sym.name for sym in missing[:6])),
            remedy=f"Document them (`{s.command} --missing {path}` lists each), then "
                   f"`{s.command} --update-baseline`."))
    return out


_PATH_LINE = re.compile(r"^(.+):(\d+)$")


def _impact_location(graph: Any, issue: Any) -> tuple[str, int]:
    """Where an impact issue points. `evidence` is `path:line` for most issues, but for
    others it is a bare path, a node label, or not a location at all — SIMILAR_FUNCTION_BODY
    carries `python_ast_body:<hex>`, and an unanchored `^(.*?):(\\d+)` read a hash that
    starts with digits as a line number in a file called `python_ast_body`. So evidence
    counts as a location only when its path is one of the issue's own nodes; otherwise
    the first node that has a path locates it."""
    nodes = [graph.nodes[n] for n in issue.node_ids if n in graph.nodes]
    match = _PATH_LINE.match(issue.evidence or "")
    if match and match.group(1) in {n.path for n in nodes if n.path}:
        return match.group(1), int(match.group(2))
    located = next((n for n in nodes if n.path), None)
    if located is not None:
        return located.path, located.line or 0
    return issue.evidence or "", 0


def _impact(ctx: Context) -> list[Finding]:
    from ..impact.config import Config as ImpactConfig
    from ..impact.scanner import scan_repository

    graph = scan_repository(ctx.root, ImpactConfig.load(ctx.root))
    mapping = {"error": ("high", "medium"), "warning": ("medium", "low"), "info": ("info", "low")}
    out = []
    for issue in graph.issues:
        severity, confidence = mapping.get(issue.severity, ("low", "low"))
        file, line = _impact_location(graph, issue)
        out.append(Finding(tool="impact", rule=f"impact/{issue.code}", severity=severity,
                           confidence=confidence, category="traceability", file=file, line=line,
                           message=issue.message, remedy=issue.recommendation or ""))
    return out


# ── external tools (optional; skipped with a reason when absent) ─────────────────
def _json_output(proc: subprocess.CompletedProcess, tool: str, ok: tuple[int, ...] = (0, 1)) -> Any:
    """A tool's JSON stdout, or an exception. Scanners conventionally exit 0 for "clean"
    and 1 for "found something"; anything else, or output that is not JSON, is a crash,
    and `proc.stdout or "{}"` would have turned that crash into a clean result."""
    detail = _tail(proc.stderr or proc.stdout or "", 3, 300)
    if proc.returncode not in ok:
        raise RuntimeError(f"{tool} exited {proc.returncode}: {detail}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"{tool} printed no JSON (exit {proc.returncode}): {detail}") from None


def _unscanned(tool: str, file: str, reason: str) -> Finding:
    return Finding(tool=tool, rule=f"{tool}/could-not-scan", severity="medium", confidence="high",
                   category="security", file=file, message=f"{tool} could not scan this file: {reason}",
                   remedy="Fix what stops it parsing; until then no rule of this tool sees the file.")


_RUFF_SEVERITY = [("S608", "high"), ("S105", "high"), ("S106", "high"), ("S107", "high"),
                  ("S301", "high"), ("S307", "high"), ("S5", "medium"), ("S3", "medium"),
                  ("S6", "low"), ("S1", "low"), ("S", "medium"), ("ASYNC", "medium"),
                  ("PERF", "low"), ("B", "low")]


def _ruff(ctx: Context) -> list[Finding]:
    exe = ctx.executable("ruff")
    select = ",".join(ctx.section["ruff_select"])
    ignore = ["--ignore", ",".join(ctx.section["ruff_ignore"])] if ctx.section["ruff_ignore"] else []
    proc = subprocess.run([exe, "check", "--output-format", "json", "--exit-zero", "--no-cache",
                           "--select", select, *ignore, *ctx.python_roots()],
                          cwd=ctx.root, capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip()[-400:])
    out = []
    for item in json.loads(proc.stdout or "[]"):
        code = item.get("code")
        if not code:
            # ruff reports a file it cannot parse with no rule code. Ranking that as a low
            # "ruff/ruff" note buried the one result meaning NO rule saw the file.
            out.append(_unscanned("ruff", _rel(ctx, item.get("filename", "")),
                                  str(item.get("message", ""))[:300]))
            continue
        severity = next(sev for prefix, sev in _RUFF_SEVERITY if code.startswith(prefix)) \
            if any(code.startswith(p) for p, _ in _RUFF_SEVERITY) else "low"
        category = ("security" if code.startswith("S") else
                    "performance" if code.startswith(("ASYNC", "PERF")) else "correctness")
        out.append(Finding(tool="ruff", rule=f"ruff/{code}", severity=severity,
                           confidence="high" if code.startswith("ASYNC") else "medium",
                           category=category, file=_rel(ctx, item.get("filename", "")),
                           line=(item.get("location") or {}).get("row", 0),
                           message=item.get("message", ""), remedy=item.get("url") or ""))
    return out


def _bandit(ctx: Context) -> list[Finding]:
    exe = ctx.executable("bandit")
    skip = merge({"skip_parts": []}, ctx.cfg.section("scan"))["skip_parts"]
    proc = subprocess.run([exe, "-r", *ctx.python_roots(), "-f", "json", "-q",
                           "-x", ",".join(f"*/{p}/*" for p in skip)],
                          cwd=ctx.root, capture_output=True, text=True, timeout=900)
    data = _json_output(proc, "bandit")
    out = [_unscanned("bandit", _rel(ctx, e.get("filename", "")), str(e.get("reason", "")))
           for e in data.get("errors", [])]
    for r in data.get("results", []):
        # bandit can say UNDEFINED; an unknown level must degrade to low, not raise and
        # error the whole tool over one result.
        severity = str(r.get("issue_severity", "LOW")).lower()
        confidence = str(r.get("issue_confidence", "LOW")).lower()
        out.append(Finding(tool="bandit", rule=f"bandit/{r.get('test_id')}-{r.get('test_name')}",
                           severity=severity if severity in ("high", "medium", "low") else "low",
                           confidence=confidence if confidence in ("high", "medium", "low") else "low",
                           category="security", file=_rel(ctx, r.get("filename", "")),
                           line=r.get("line_number", 0), message=r.get("issue_text", ""),
                           remedy=r.get("more_info", "")))
    return out


def _semgrep(ctx: Context) -> list[Finding]:
    config = ctx.section["semgrep_config"]
    if not config:
        raise Skip("set [report] semgrep_config to a LOCAL rules path (registry configs need the network)")
    exe = ctx.executable("semgrep")
    proc = subprocess.run([exe, "scan", "--config", config, "--json", "--metrics=off", "--quiet",
                           *ctx.python_roots()], cwd=ctx.root, capture_output=True, text=True, timeout=1800)
    data = _json_output(proc, "semgrep")
    level = {"ERROR": "high", "WARNING": "medium", "INFO": "low"}
    out = []
    for err in data.get("errors", []):
        message = str(err.get("message") or err.get("type") or err)[:300]
        if not err.get("path"):  # a rule or config error: the scan itself is broken
            raise RuntimeError(f"semgrep reported an error: {message}")
        out.append(_unscanned("semgrep", _rel(ctx, err["path"]), message))
    for r in data.get("results", []):
        extra = r.get("extra", {})
        meta = extra.get("metadata", {})
        out.append(Finding(tool="semgrep", rule=f"semgrep/{r.get('check_id')}",
                           severity=level.get(extra.get("severity", ""), "medium"),
                           confidence=str(meta.get("confidence", "medium")).lower()
                           if str(meta.get("confidence", "")).lower() in ("high", "medium", "low") else "medium",
                           category="security", file=_rel(ctx, r.get("path", "")),
                           line=(r.get("start") or {}).get("line", 0), message=extra.get("message", "")))
    return out


def _osv(ctx: Context) -> list[Finding]:
    exe = ctx.executable("osv-scanner")
    proc = subprocess.run([exe, "--format", "json", "-r", "."], cwd=ctx.root,
                          capture_output=True, text=True, timeout=900)
    if proc.returncode == 128:  # osv-scanner's "no packages found"
        raise Skip("osv-scanner found no lockfile or manifest to scan")
    data = _json_output(proc, "osv-scanner")
    out = []
    for result in data.get("results", []):
        source = (result.get("source") or {}).get("path", "")
        for pkg in result.get("packages", []):
            info = pkg.get("package", {})
            for vuln in pkg.get("vulnerabilities", []):
                sev = str((vuln.get("database_specific") or {}).get("severity", "high")).lower()
                sev = {"moderate": "medium"}.get(sev, sev)  # GitHub advisories say MODERATE
                out.append(Finding(tool="osv-scanner", rule=f"deps/{vuln.get('id')}",
                                   severity=sev if sev in ("critical", "high", "medium", "low") else "high",
                                   confidence="high", category="dependencies", file=_rel(ctx, source),
                                   message=f"{info.get('name')} {info.get('version')}: {vuln.get('summary') or vuln.get('id')}",
                                   remedy="Upgrade to a fixed version."))
    return out


def _pip_audit(ctx: Context) -> list[Finding]:
    exe = ctx.executable("pip-audit")
    requirements = ctx.section["requirements"]
    if not requirements:
        raise Skip("set [report] requirements to the requirements files to audit")
    out = []
    for req in requirements:
        proc = subprocess.run([exe, "-r", req, "-f", "json", "--progress-spinner", "off"],
                              cwd=ctx.root, capture_output=True, text=True, timeout=900)
        data = _json_output(proc, "pip-audit")
        for dep in data.get("dependencies", []):
            for vuln in dep.get("vulns", []):
                fixes = ", ".join(vuln.get("fix_versions") or []) or "none published"
                out.append(Finding(tool="pip-audit", rule=f"deps/{vuln.get('id')}", severity="high",
                                   confidence="high", category="dependencies", file=req,
                                   message=f"{dep.get('name')} {dep.get('version')}: {vuln.get('id')}",
                                   remedy=f"Upgrade to: {fixes}"))
    return out


ADAPTERS: dict[str, Callable[[Context], list[Finding]]] = {
    "security": _security, "performance": _performance, "migrations": _migrations,
    "featuretrace": _featuretrace,
    "owners": _owners, "gates": _gates, "artefacts": _artefacts, "docstrings": _docstrings,
    "impact": _impact,
    "ruff": _ruff, "bandit": _bandit, "semgrep": _semgrep, "osv-scanner": _osv,
    "pip-audit": _pip_audit,
}


# ── configured commands: a repository's own checks join the report ───────────────
def _tail(text: str, lines: int = 10, width: int = 700) -> str:
    kept = [ln.strip() for ln in text.splitlines() if ln.strip()][-lines:]
    joined = " ⏎ ".join(kept)
    return joined if len(joined) <= width else "…" + joined[-width:]


def _command_run(ctx: Context, spec: dict[str, Any], name: str) -> ToolRun:
    argv = [ctx.python() if part == "{python}" else part for part in spec["run"]]
    start = time.time()
    try:
        proc = subprocess.run(argv, cwd=ctx.root, capture_output=True, text=True,
                              timeout=ctx.section["command_timeout_seconds"])
    except FileNotFoundError:
        return ToolRun(name, skipped=f"{argv[0]} not found")
    except subprocess.TimeoutExpired:
        return ToolRun(name, error="timed out", seconds=time.time() - start)
    run = ToolRun(name, seconds=time.time() - start)
    output = (proc.stdout or "") + "\n" + (proc.stderr or "")
    if proc.returncode == 2 and ("usage:" in output or "unrecognized arguments" in output):
        run.error = "the command line was rejected: " + _tail(proc.stderr, 3, 300)
    elif proc.returncode != 0 and "Traceback (most recent call last)" in output:
        # A check that CRASHED has not reported a finding. Recording it as one would let
        # --update-baseline accept a broken gate, which then passes --check forever.
        run.error = f"crashed (exit {proc.returncode}): " + _tail(output, 4, 400)
    elif proc.returncode != 0:
        script = next((p for p in spec["run"] if p.endswith((".py", ".mjs", ".sh"))), "")
        run.findings.append(Finding(
            tool=name, rule=f"check/{spec['name']}", severity=spec.get("severity", "medium"),
            confidence=spec.get("confidence", "high"), category=spec.get("category", "hygiene"),
            file=script, message=f"`{' '.join(spec['run'])}` failed: " + _tail(output),
            remedy=spec.get("remedy", "Run it and follow its output."),
            # A ratchet reports its count, and a changed count is a changed finding.
            counts_matter=True,
        ))
    return run


def _sarif_command_run(ctx: Context, spec: dict[str, Any], name: str) -> list[ToolRun]:
    """Run one analyser that writes SARIF, then import what it wrote.

    `{output}` in the argv is replaced with a temporary file path. The file is read back
    through `import_sarif`, which never opens anything the document references, so this
    adds a tool's findings without giving it a say in what repolens reads.
    """
    from .sarif import import_sarif
    if not any("{output}" in part for part in spec["run"]):
        return [ToolRun(name, error="the run argv must contain {output}, the SARIF path to write")]
    ok_exit = tuple(spec.get("ok_exit", (0, 1)))  # scanners exit 1 for "found something"
    with tempfile.TemporaryDirectory() as workspace:
        output = Path(workspace) / "report.sarif"
        argv = [part.replace("{output}", str(output)) for part in spec["run"]]
        argv = [ctx.python() if part == "{python}" else part for part in argv]
        start = time.time()
        try:
            proc = subprocess.run(argv, cwd=ctx.root, capture_output=True, text=True,
                                  timeout=ctx.section["command_timeout_seconds"])
        except FileNotFoundError:
            return [ToolRun(name, skipped=f"{argv[0]} not found")]
        except subprocess.TimeoutExpired:
            return [ToolRun(name, error="timed out", seconds=time.time() - start)]
        seconds = time.time() - start
        if proc.returncode not in ok_exit:
            detail = _tail((proc.stderr or "") + "\n" + (proc.stdout or ""), 4, 400)
            return [ToolRun(name, error=f"exited {proc.returncode}: {detail}", seconds=seconds)]
        if not output.is_file():
            # Exit 0 and no document is a tool that did not look, not a clean repository.
            return [ToolRun(name, error="the command exited cleanly but wrote no SARIF to {output}",
                            seconds=seconds)]
        runs = import_sarif(output, ctx.root)
        for run in runs:
            # One run keeps the configured name, so `--require gitleaks` can match it. A
            # document with several runs qualifies each by its driver.
            run.tool = name if len(runs) == 1 else f"{name}:{run.tool}"
            run.seconds = seconds
        return runs or [ToolRun(name, seconds=seconds)]


def _sarif_command_runs(ctx: Context) -> list[ToolRun]:
    runs = []
    for spec in ctx.section["sarif_commands"]:
        name = spec.get("name") or "sarif"
        try:
            runs.extend(_sarif_command_run(ctx, spec, name))
        except (Exception, SystemExit) as exc:  # one bad entry must not sink the report
            runs.append(ToolRun(name, error=f"{type(exc).__name__}: {exc}"[:400]))
    return runs


def _command_runs(ctx: Context) -> list[ToolRun]:
    runs = []
    for spec in ctx.section["commands"]:
        name = f"cmd:{spec.get('name', '?')}"
        try:
            runs.append(_command_run(ctx, spec, name))
        except (Exception, SystemExit) as exc:  # one bad entry must not sink the report
            runs.append(ToolRun(name, error=f"{type(exc).__name__}: {exc}"[:400]))
    return runs


def _commit(root: Path) -> str:
    try:
        sha = run_git(root, "rev-parse", "--short", "HEAD", check=True, timeout=30).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    # Not `git status`, which runs the target's clean filters; unknown is never shown as clean.
    modified = is_modified(root)
    return sha + ("+dirty" if modified else "" if modified is False else "+unknown")


def main(argv: list[str] | None = None, *, config: Config | None = None, prog: str | None = None) -> int:
    ap = argparse.ArgumentParser(prog=prog, description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", help="comma-separated tools to run instead of [report] tools")
    ap.add_argument("--with", dest="extra", default="",
                    help=f"add optional tools: {', '.join(OPTIONAL_TOOLS)}")
    ap.add_argument("--out", help="output directory (default [report] out_dir)")
    ap.add_argument("--check", action="store_true",
                    help="exit 1 on a NEW medium/high-confidence finding at --fail-on or worse")
    ap.add_argument("--fail-on", choices=PRIORITIES)
    ap.add_argument("--update-baseline", action="store_true",
                    help="record every current finding as known")
    ap.add_argument("--require", default="",
                    help="comma-separated tools that must run: one skipped, errored or not "
                         "selected exits 1 in every mode (and refuses --update-baseline). Also "
                         "added to [report] require for --check (CI names the tools it installed)")
    ap.add_argument("--list-tools", action="store_true")
    ap.add_argument("--sarif", action="append", default=[], metavar="FILE",
                    help="import local SARIF 2.1.0 findings; repeat for multiple files")
    args = ap.parse_args(argv)
    build = tool_build()  # at start-up: stamp the code that ran, not a later checkout
    if args.check and args.update_baseline:
        # Writing the baseline first makes every finding known, so the check could only pass.
        ap.error("--check with --update-baseline always passes; run them separately")

    cfg = config or load_config()
    section = merge(DEFAULTS, cfg.section("report"))
    ctx = Context(cfg, section)
    if args.list_tools:
        print("default: " + ", ".join(section["tools"]))
        print("all:     " + ", ".join([*ADAPTERS, *_PSEUDO_TOOLS]))
        return 0

    tools = args.only.split(",") if args.only else list(section["tools"])
    tools += [t for t in args.extra.split(",") if t and t not in tools]
    unknown = [t for t in tools if t not in ADAPTERS and t not in _PSEUDO_TOOLS]
    if unknown:
        print(f"unknown tool(s): {', '.join(unknown)}; see --list-tools", file=sys.stderr)
        return 2

    runs: list[ToolRun] = []
    from .sarif import import_sarif
    for filename in args.sarif:
        try:
            sarif_path = Path(filename)
            if not sarif_path.is_absolute():
                sarif_path = ctx.root / sarif_path
            runs.extend(import_sarif(sarif_path, ctx.root))
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            runs.append(ToolRun("sarif", error=f"Cannot import {filename}: {exc}"[:400]))
    for name in tools:
        start = time.time()
        if name == "commands":
            runs.extend(_command_runs(ctx))
            print(f"  commands      {time.time() - start:6.1f}s", file=sys.stderr)
            continue
        if name == "sarif-commands":
            # Selected like any other tool, so `--only lessons` does not shell out to
            # trivy and `--list-tools` shows the seam exists.
            runs.extend(_sarif_command_runs(ctx))
            print(f"  sarif-commands{time.time() - start:6.1f}s", file=sys.stderr)
            continue
        try:
            run = ToolRun(name, findings=ADAPTERS[name](ctx))
        except Skip as skip:
            run = ToolRun(name, skipped=str(skip))
        except (Exception, SystemExit) as exc:  # a crashed tool is reported, never a silent zero
            run = ToolRun(name, error=f"{type(exc).__name__}: {exc}"[:400])
        run.seconds = time.time() - start
        runs.append(run)
        status = run.skipped and "skipped" or run.error and "ERROR" or f"{len(run.findings)} findings"
        print(f"  {name:<13} {run.seconds:6.1f}s  {status}", file=sys.stderr)

    # A tool the command line requires must have RUN: skipped, errored and never selected
    # all mean it did not look, and `ruff skipped` scrolling past a green exit was how an
    # installed tool silently dropped out of a CI report.
    demanded = [t for t in dict.fromkeys(args.require.split(",")) if t]
    ran = {r.tool for r in runs if not r.skipped and not r.error}
    unmet = [t for t in demanded if t not in ran]
    if unmet:
        why = {r.tool: r.skipped or r.error for r in runs}
        for tool in unmet:
            print(f"required tool {tool} did not run: {why.get(tool) or 'not selected'}", file=sys.stderr)

    suppress = section["suppress"]
    for run in runs:
        run.findings = [f for f in run.findings if f.fingerprint not in suppress]
    findings = sorted((f for r in runs for f in r.findings), key=sort_key)

    baseline_path = ctx.root / section["baseline"]
    if args.update_baseline:
        if any(run.error for run in runs):
            print("Cannot update the baseline: one or more tools failed; fix the incomplete run first.", file=sys.stderr)
            return 2
        if unmet:
            print(f"Cannot update the baseline: required tool(s) did not run: {', '.join(unmet)}.",
                  file=sys.stderr)
            return 2
        write_baseline(baseline_path, findings)
        print(f"baseline: {len(findings)} findings recorded in {baseline_path.relative_to(ctx.root)}")
    try:
        baseline = load_baseline(baseline_path)
    except BaselineError as exc:
        print(f"repolens report: unusable baseline: {exc}\n"
              "Regenerate it with --update-baseline (and review what that accepts).", file=sys.stderr)
        return 2
    mark_new(findings, baseline, load_magnitudes(baseline_path))

    meta = {"title": section["title"], "commit": _commit(ctx.root), "baseline": baseline is not None,
            "tools": tools, "repolens": __version__, "suppressed": len(suppress),
            "produced_by": describe_build(build)}
    out_dir = Path(args.out) if args.out else ctx.root / section["out_dir"]
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.md").write_text(to_markdown(runs, meta), encoding="utf-8")
    (out_dir / "report.json").write_text(to_json(runs, meta), encoding="utf-8")
    (out_dir / "report.sarif").write_text(to_sarif(runs, __version__, build=build), encoding="utf-8")
    # The report makes no whole-analysis completeness claim: each skipped or errored tool is
    # shown as such in its own run.
    (out_dir / "report.html").write_text(
        render_html(runs, title=section["title"], repository=ctx.root.name, complete=None,
                    build=describe_build(build), baseline=baseline is not None), encoding="utf-8")

    s = summary(findings)
    print(f"\n{s['total']} findings: " + ", ".join(f"{p} {n}" for p, n in s["by_priority"].items())
          + (f"  ({s['new']} new)" if baseline is not None else "  (no baseline)"))
    for f in [f for f in findings if f.priority in ("P0", "P1")][:12]:
        print(f"  {f.priority} {f.severity:<8} {f.rule:<48} {f.location}")
    print(f"\nwrote {out_dir}/report.md, report.html, report.json, report.sarif")

    if args.check:
        if baseline is None:
            print("--check needs a baseline: run with --update-baseline first", file=sys.stderr)
            return 1
        worst = PRIORITIES.index(args.fail_on or section["fail_on"])
        blocking = [f for f in findings if f.new and f.confidence != "low"
                    and PRIORITIES.index(f.priority) <= worst]
        errored = [r.tool for r in runs if r.error]
        required = set(section["require"]) | {t for t in args.require.split(",") if t}
        missing = sorted(required - {r.tool for r in runs if not r.skipped})
        for f in blocking:
            print(f"NEW {f.priority} {f.rule} {f.location}: {f.message}", file=sys.stderr)
        if errored:
            print(f"tool(s) errored, so the report is incomplete: {', '.join(errored)}", file=sys.stderr)
        if missing:
            print(f"required tool(s) did not run: {', '.join(missing)}", file=sys.stderr)
        return 1 if blocking or errored or missing or unmet else 0
    return 1 if unmet else 0


if __name__ == "__main__":
    raise SystemExit(main())

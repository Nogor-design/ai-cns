"""The Phase 3 verification gate: what must be true before work merges.

Unattended write work is only safe if something other than the agent decides
whether its change is acceptable. That decision is made here, once, in a fixed
order, and every check records the evidence it judged on:

1. **changes** - the run actually produced a commit. Nothing to merge is a
   rejection, not a pass.
2. **path scope** - only files the task was allowed to touch.
3. **protected files** - CI, lockfiles, secrets, agent configuration and the
   project blueprint are the owner's. Touching them is not rejected outright;
   it becomes an owner decision in the inbox, because sometimes it is right.
4. **secrets** - the diff itself is scanned, not just its filenames.
5. **task tests** - the project's test command on the task branch.
6. **merge** - a real ``--no-ff`` merge into ``cortex/integration``. A conflict
   is never resolved automatically; it goes to the owner.
7. **merged tests** - the project's test command on the merged result, which is
   the only place "does it still work together" can be answered.
8. **review** - a second model judges the change against its acceptance
   criteria.

The order is deliberate: cheap deterministic checks before slow ones, and the
only check that spends tokens last. Checks 6-8 run after the merge, so if any
of them fails the integration branch is rewound to exactly where it started.
The gate never pushes, never touches ``main``, and never resolves a conflict.
"""

from __future__ import annotations

import fnmatch
import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import (
    autonomy, gitutil, ids, inbox, integration, review as review_mod,
    runs as runs_mod, secrets_scan, store, worktrees,
)

PASS, FAIL, SKIPPED, OWNER = "pass", "fail", "skipped", "owner"

# Files an agent may not change on its own authority. Each entry is a glob
# matched against the repository-relative path; the reason is what the owner
# reads in the inbox, so it says why the file matters, not what it is called.
PROTECTED_FILES: tuple[tuple[str, str], ...] = (
    (".github/workflows/*", "changes what CI runs"),
    (".github/actions/**", "changes what CI runs"),
    (".gitlab-ci.yml", "changes what CI runs"),
    ("azure-pipelines.yml", "changes what CI runs"),
    ("Jenkinsfile", "changes what CI runs"),
    ("package-lock.json", "changes resolved dependencies"),
    ("pnpm-lock.yaml", "changes resolved dependencies"),
    ("yarn.lock", "changes resolved dependencies"),
    ("poetry.lock", "changes resolved dependencies"),
    ("uv.lock", "changes resolved dependencies"),
    ("Cargo.lock", "changes resolved dependencies"),
    ("requirements*.txt", "changes resolved dependencies"),
    (".env*", "holds credentials"),
    ("*secrets*", "holds credentials"),
    ("*credentials*", "holds credentials"),
    ("*.pem", "holds credentials"),
    ("*.key", "holds credentials"),
    ("CLAUDE.md", "is agent instruction, which changes how every later run behaves"),
    ("AGENTS.md", "is agent instruction, which changes how every later run behaves"),
    (".claude/**", "is agent configuration"),
    (".codex/**", "is agent configuration"),
    (".cursor/**", "is agent configuration"),
    (".cortex/blueprint.md", "is the approved project blueprint"),
    (".cortex/state.md", "is the project state document Cortex maintains"),
)

_TEST_TIMEOUT_NOTE = "no test command is configured for this project"


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.status in {FAIL, OWNER}

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "status": self.status,
            "detail": self.detail, "evidence": self.evidence,
        }


@dataclass
class GateReport:
    task_id: str
    project_id: str
    run_id: str | None
    status: str = "rejected"        # merged | verified | rejected | needs_owner
    checks: list[Check] = field(default_factory=list)
    task_branch: str | None = None
    merge_commit: str | None = None
    integration_branch: str = integration.BRANCH
    verification_id: str | None = None

    @property
    def merged(self) -> bool:
        return self.status == "merged"

    def add(self, check: Check) -> Check:
        self.checks.append(check)
        return check

    def blockers(self) -> list[Check]:
        return [check for check in self.checks if check.blocking]

    def summary(self) -> str:
        blockers = self.blockers()
        if self.status == "merged":
            return f"Merged into {self.integration_branch} ({(self.merge_commit or '')[:8]})."
        if self.status == "verified":
            return f"Passed every check; waiting on {self.task_branch}."
        if not blockers:
            return "Not merged."
        return "; ".join(f"{check.name}: {check.detail}" for check in blockers)[:1000]

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.verification_id, "task_id": self.task_id,
            "project_id": self.project_id, "run_id": self.run_id,
            "status": self.status, "checks": [check.as_dict() for check in self.checks],
            "task_branch": self.task_branch, "merge_commit": self.merge_commit,
            "integration_branch": self.integration_branch, "summary": self.summary(),
        }


def path_violations(changed: list[str], allowed: str | None) -> list[str]:
    """Changed paths outside the task's declared scope (empty when unscoped)."""
    patterns = _patterns(allowed)
    if not patterns:
        return []
    return [
        path for path in changed
        if not any(_matches(path, pattern) for pattern in patterns)
    ]


def protected_changes(changed: list[str]) -> list[dict[str, str]]:
    """Protected files this change touches, with why each one is protected."""
    hits: list[dict[str, str]] = []
    for path in changed:
        for pattern, reason in PROTECTED_FILES:
            if _matches(path, pattern):
                hits.append({"path": path, "reason": reason})
                break
    return hits


def verify(
    conn: sqlite3.Connection,
    task: sqlite3.Row,
    *,
    project: sqlite3.Row,
    workspace: str | Path,
    before: str | None,
    producer: str,
    run_id: str | None = None,
    unattended: bool = True,
    may_merge: bool = True,
    review_fn: Callable[..., review_mod.ReviewOutcome] = review_mod.run,
    test_fn: Callable[[str | Path, str], int] = runs_mod.execute_test_command,
) -> GateReport:
    """Judge one write run and, if every check passes, merge it. Never pushes.

    ``may_merge`` is false for projects the owner has not put in ``integration``
    mode. The checks still run and are still recorded — knowing a change would
    have passed is worth having — but the work waits on its own branch.
    """
    report = GateReport(task_id=task["id"], project_id=project["id"], run_id=run_id)
    workspace = Path(workspace)
    test_command = project["test_command"]

    # 1. changes
    try:
        committed = worktrees.commit_work(
            workspace, f"cortex: {task['title']}"[:200]
        )
    except worktrees.WorktreeError as exc:
        report.add(Check("changes", FAIL, f"could not commit the agent's work: {exc}"))
        return _finish(conn, report, project, task)
    after = gitutil.head(workspace)
    report.task_branch = gitutil.branch(workspace)
    # Judge what the merge would introduce, not what changed since this run
    # started. A retried task reuses its worktree, so an earlier attempt's
    # commit is already in it; comparing against the dispatch-time HEAD made
    # that work invisible and the gate reported "no change" on a branch that
    # plainly had one. The merge base with the integration branch is the same
    # revision for a fresh worktree and the right one for a resumed branch.
    before = _merge_base(workspace, integration.BRANCH) or before
    changed = _committed_files(workspace, before, after)
    if not changed or after == before:
        report.add(Check(
            "changes", FAIL, "the run produced no committed change to merge",
            {"committed": committed, "head": after},
        ))
        return _finish(conn, report, project, task)
    report.add(Check(
        "changes", PASS, f"{len(changed)} file(s) committed on {report.task_branch}",
        {"files": changed[:100], "commit": after, "committed_by_cortex": bool(committed)},
    ))

    # 2. path scope
    violations = path_violations(changed, task["allowed_paths"])
    report.add(Check(
        "path_scope",
        FAIL if violations else PASS,
        (f"{len(violations)} file(s) outside the task's allowed paths"
         if violations else "every changed file is inside the task's scope"),
        {"violations": violations, "allowed": task["allowed_paths"]},
    ))

    # 3. protected files — an owner decision, not a silent refusal
    protected = protected_changes(changed)
    report.add(Check(
        "protected_files",
        OWNER if protected else PASS,
        (f"{len(protected)} protected file(s) changed; the owner decides"
         if protected else "no protected file was touched"),
        {"protected": protected},
    ))

    # 4. secrets in the diff itself
    diff = gitutil.diff_text(workspace, before, after)
    findings = secrets_scan.scan(_diff_content(diff), use_ollama=False)
    report.add(Check(
        "secrets",
        FAIL if findings else PASS,
        (f"{len(findings)} possible secret(s) in the diff"
         if findings else "no secret pattern in the diff"),
        {"findings": [str(item) for item in findings][:20]},
    ))

    # 5. task tests
    report.add(_test_check("task_tests", workspace, test_command, test_fn))

    if report.blockers():
        return _finish(conn, report, project, task)
    if not may_merge:
        report.status = "verified"
        report.add(Check(
            "merge", SKIPPED,
            f"checks passed; not merged because this project is not in "
            f"'integration' autonomy mode. The work is on {report.task_branch}.",
        ))
        return _finish(conn, report, project, task)

    # 6. merge (real, --no-ff, rewound below if a later check fails)
    try:
        workspace_int = integration.ensure(project["repo_path"], project["id"])
        refreshed = integration.refresh(workspace_int)
        base_before = gitutil.head(workspace_int.path)
        outcome = integration.merge(
            workspace_int, report.task_branch or "",
            message=f"cortex: {task['title']}\n\nTask: {task['id']}\nRun: {run_id or '-'}",
        )
    except integration.IntegrationError as exc:
        report.add(Check("merge", FAIL, str(exc)))
        return _finish(conn, report, project, task)
    if outcome.status == "conflict":
        report.add(Check(
            "merge", OWNER,
            f"conflicts with {integration.BRANCH} in {len(outcome.conflicts)} file(s); "
            "Cortex does not resolve conflicts",
            {"conflicts": list(outcome.conflicts), "base_state": refreshed},
        ))
        return _finish(conn, report, project, task)
    if not outcome.ok:
        report.add(Check("merge", FAIL, outcome.detail or "the merge failed", {}))
        return _finish(conn, report, project, task)
    report.merge_commit = outcome.commit
    report.add(Check(
        "merge", PASS, f"merged --no-ff into {integration.BRANCH}",
        {"commit": outcome.commit, "base_state": refreshed, "base": workspace_int.base},
    ))

    # Steps 7 and 8 run with the merge already on the branch, so anything that
    # goes wrong in them -- including a bug in the gate itself -- must rewind
    # it. A real run crashed here after merging and left the branch advanced
    # with nothing recorded, which is the one outcome this module must never
    # produce.
    try:
        # 7. tests on the merged result
        merged_tests = report.add(
            _test_check("merged_tests", workspace_int.path, test_command, test_fn)
        )

        # 8. second-model review
        if merged_tests.status == FAIL:
            report.add(
                Check("review", SKIPPED, "not reviewed: the merged result fails its tests")
            )
        else:
            verdict = review_fn(
                conn, project, task, diff=diff, changed_files=changed,
                producer=producer, workspace=workspace, unattended=unattended,
                verified=_verified_facts(report),
                # High-risk work is judged by a premium model whatever its
                # size; the cheap tier is for small, ordinary changes.
                premium_required=str(_task_field(task, "risk")).lower() == "high",
            )
            report.add(Check(
                "review",
                PASS if verdict.ok else FAIL,
                (f"{verdict.reviewer or 'review'}: " + (
                    "; ".join(verdict.reasons) or verdict.status))[:500],
                verdict.as_dict(),
            ))
    except Exception as exc:  # the branch must not keep an unjudged merge
        integration.rollback(workspace_int, base_before)
        report.merge_commit = None
        report.add(Check(
            "gate", FAIL,
            f"the gate failed after merging and the branch was rewound: "
            f"{type(exc).__name__}: {exc}"[:500],
            {"reset_to": base_before},
        ))
        report.status = "error"
        return _finish(conn, report, project, task)

    if report.blockers():
        integration.rollback(workspace_int, base_before)
        report.merge_commit = None
        report.add(Check(
            "rollback", PASS,
            f"{integration.BRANCH} rewound to {base_before[:8] if base_before else '?'}; "
            "nothing was kept",
            {"reset_to": base_before},
        ))
        return _finish(conn, report, project, task)

    report.status = "merged"
    return _finish(conn, report, project, task)


def _verified_facts(report: GateReport) -> tuple[str, ...]:
    """What the gate has already proved, phrased for the reviewer."""
    facts = []
    for check in report.checks:
        if check.status != PASS:
            continue
        if check.name == "path_scope":
            facts.append("Every changed file is inside the task's allowed paths.")
        elif check.name == "protected_files":
            facts.append("No protected file was touched.")
        elif check.name == "secrets":
            facts.append("The diff was scanned for secrets and none were found.")
        elif check.name == "task_tests":
            facts.append(f"{check.detail} (run by Cortex, not claimed by the author).")
        elif check.name == "merged_tests":
            facts.append(f"{check.detail} - the tests also pass on the merged result.")
    return tuple(facts)


def _task_field(task: sqlite3.Row, name: str) -> str:
    try:
        return str(task[name] or "")
    except (IndexError, KeyError):
        return ""


def merge_allowed(project: sqlite3.Row) -> bool:
    """Only projects the owner put in ``integration`` mode get an auto-merge."""
    return autonomy.mode(project) == "integration"


def _finish(
    conn: sqlite3.Connection, report: GateReport, project: sqlite3.Row, task: sqlite3.Row
) -> GateReport:
    if report.status not in {"merged", "verified", "error"}:
        report.status = "needs_owner" if any(
            check.status == OWNER for check in report.checks
        ) else "rejected"
    report.verification_id = record(conn, report)
    store.create_activity_event(
        conn,
        project_id=project["id"],
        task_id=task["id"],
        actor_type="system",
        actor_name="cortex-gate",
        action=f"verification.{report.status}",
        summary=report.summary()[:500],
        source="cortex-gate",
        source_ref=report.run_id or report.verification_id,
        evidence={"checks": [check.as_dict() for check in report.checks]},
    )
    if report.status == "needs_owner":
        owner_checks = [check for check in report.checks if check.status == OWNER]
        inbox.add(
            conn,
            kind="verification_decision",
            title=f"Needs your decision before merging: {task['title']}",
            detail="\n".join(f"{check.name}: {check.detail}" for check in owner_checks),
            project_id=project["id"], task_id=task["id"], run_id=report.run_id,
            dedupe_key=f"gate:{report.verification_id}",
        )
    elif report.status in {"rejected", "error"}:
        inbox.add(
            conn,
            kind="verification_rejected",
            title=f"Agent work was not merged: {task['title']}",
            detail=report.summary(),
            project_id=project["id"], task_id=task["id"], run_id=report.run_id,
            dedupe_key=f"gate:{report.verification_id}",
        )
    return report


def record(conn: sqlite3.Connection, report: GateReport) -> str:
    verification_id = ids.short_id()
    conn.execute(
        """INSERT INTO verifications (id, run_id, task_id, project_id, status,
                                      checks_json, task_branch, integration_branch,
                                      merge_commit, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            verification_id, report.run_id, report.task_id, report.project_id,
            report.status, json.dumps([check.as_dict() for check in report.checks]),
            report.task_branch, report.integration_branch, report.merge_commit, ids.now(),
        ),
    )
    conn.commit()
    return verification_id


def get(conn: sqlite3.Connection, verification_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        """SELECT v.*, t.title AS task_title, p.name AS project_name
           FROM verifications v
           LEFT JOIN tasks t ON t.id = v.task_id
           LEFT JOIN projects p ON p.id = v.project_id
           WHERE v.id = ?""",
        (verification_id,),
    ).fetchone()
    return _row(row) if row else None


def for_run(conn: sqlite3.Connection, run_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM verifications WHERE run_id = ? ORDER BY created_at DESC LIMIT 1",
        (run_id,),
    ).fetchone()
    return _row(row) if row else None


def recent(
    conn: sqlite3.Connection, *, project_id: str | None = None, limit: int = 20
) -> list[dict[str, Any]]:
    where = "WHERE v.project_id = ?" if project_id else ""
    params: tuple[Any, ...] = (project_id, limit) if project_id else (limit,)
    rows = conn.execute(
        f"""SELECT v.*, t.title AS task_title, p.name AS project_name
            FROM verifications v
            LEFT JOIN tasks t ON t.id = v.task_id
            LEFT JOIN projects p ON p.id = v.project_id
            {where}
            ORDER BY v.created_at DESC LIMIT ?""",
        params,
    ).fetchall()
    return [_row(row) for row in rows]


def mark_reverted(conn: sqlite3.Connection, verification_id: str, commit: str) -> None:
    conn.execute(
        "UPDATE verifications SET reverted_at = ?, revert_commit = ? WHERE id = ?",
        (ids.now(), commit, verification_id),
    )
    conn.commit()


def _row(row: sqlite3.Row) -> dict[str, Any]:
    item = {key: row[key] for key in row.keys() if key != "checks_json"}
    try:
        item["checks"] = json.loads(row["checks_json"])
    except (json.JSONDecodeError, TypeError):
        item["checks"] = []
    return item


def _test_check(
    name: str,
    workspace: str | Path,
    command: str | None,
    test_fn: Callable[[str | Path, str], int],
) -> Check:
    if not command:
        # A project with no test command is not a pass; it is an absence, and
        # the owner should see it as one rather than as a green check.
        return Check(name, SKIPPED, _TEST_TIMEOUT_NOTE)
    passed = test_fn(workspace, command)
    return Check(
        name, PASS if passed == 1 else FAIL,
        f"`{command}` {'passed' if passed == 1 else 'failed'} in {Path(workspace).name}",
        {"command": command, "passed": bool(passed)},
    )


def _diff_content(diff: str) -> str:
    """Only the lines the change actually adds or removes.

    Git's own metadata is not part of anyone's change, and scanning it cost a
    real, correct change its merge: the blob hashes in `index e9582a2..5536435`
    matched the credit-card pattern, and the run was rejected for leaking a
    secret it did not contain.
    """
    lines = []
    for line in diff.splitlines():
        if line.startswith(("+++", "---")):
            continue
        if line.startswith(("+", "-")):
            lines.append(line[1:])
    return "\n".join(lines)


def _merge_base(workspace: Path, branch: str) -> str | None:
    """The commit a task branch and the integration branch last shared."""
    code, out, _ = gitutil._run(workspace, "merge-base", branch, "HEAD")
    return out.strip() or None if code == 0 else None


def _committed_files(workspace: Path, before: str | None, after: str | None) -> list[str]:
    if not before or not after:
        return []
    code, out, _ = gitutil._run(workspace, "diff", "--name-only", f"{before}..{after}")
    return [line.strip() for line in out.splitlines() if line.strip()] if code == 0 else []


def _patterns(allowed: str | None) -> list[str]:
    if not allowed:
        return []
    try:
        parsed = json.loads(allowed)
        patterns = parsed if isinstance(parsed, list) else [str(parsed)]
    except json.JSONDecodeError:
        patterns = [part.strip() for part in re.split(r"[,\n]", allowed)]
    return [str(pattern).strip() for pattern in patterns if str(pattern).strip()]


def _matches(path: str, pattern: str) -> bool:
    """Glob match that treats ``**`` and a bare directory prefix as recursive."""
    path = path.replace("\\", "/")
    pattern = pattern.replace("\\", "/")
    if fnmatch.fnmatch(path, pattern):
        return True
    if pattern.endswith("/**") and path.startswith(pattern[:-3].rstrip("/") + "/"):
        return True
    if pattern.endswith("/") and path.startswith(pattern):
        return True
    # "src/**/*.py" style patterns should also match "src/a.py".
    if "/**/" in pattern and fnmatch.fnmatch(path, pattern.replace("/**/", "/")):
        return True
    return False

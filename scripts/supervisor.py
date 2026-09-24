#!/usr/bin/env python3
"""Secretary Bus supervisor for auditable CLI-agent work.

This layer coordinates a registered tmux target, a repo baseline, terminal
capture, and git diffs. The low-level key injection stays in cli_bridge.py.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import cli_bridge  # noqa: E402
import event_ledger  # noqa: E402


BUS = cli_bridge.BUS
JOBS = BUS / "secretary-jobs"
COMMAND_TIMEOUT = float(os.environ.get("SECRETARY_BUS_COMMAND_TIMEOUT", "20"))
MAX_UNTRACKED_DIFF_BYTES = int(os.environ.get("SECRETARY_BUS_MAX_UNTRACKED_DIFF_BYTES", "1000000"))


def now() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")


def job_id() -> str:
    return datetime.now().astimezone().strftime("%Y%m%dT%H%M%S-%f")


def run(args: list[str], cwd: Path | None = None, check: bool = False) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            args,
            cwd=str(cwd) if cwd else None,
            text=True,
            capture_output=True,
            check=check,
            timeout=COMMAND_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise SystemExit(f"command timed out after {COMMAND_TIMEOUT}s: {' '.join(args)}") from exc


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def target_for(name: str) -> cli_bridge.Target:
    targets = cli_bridge.load_targets()
    if name not in targets:
        raise SystemExit(f"unknown target: {name}")
    return targets[name]


def target_for_job(job: dict[str, object]) -> cli_bridge.Target:
    target = target_for(str(job["target"]))
    job_pane_id = str(job.get("pane_id") or "")
    mapping_changed = (
        bool(job_pane_id) and target.pane_id != job_pane_id
    ) or (
        not job_pane_id and target.pane != job.get("pane")
    )
    if mapping_changed:
        raise SystemExit(
            "target mapping changed since job start: "
            f"{job['target']} was {job_pane_id or job.get('pane')} now {target.pane_id or target.pane}"
        )
    if target.expected_command != job.get("expected_command"):
        raise SystemExit(
            "target command contract changed since job start: "
            f"{job['target']} was {job.get('expected_command')} now {target.expected_command}"
        )
    info = cli_bridge.target_info(target)
    runtime_fields = ("pane_start_time", "foreground_pid", "foreground_start_time")
    mismatched = [
        field
        for field in runtime_fields
        if job.get(field) not in (None, "", 0) and str(job.get(field)) != str(info.get(field))
    ]
    if mismatched:
        raise SystemExit(
            "target runtime changed since job start: "
            f"{job['target']} mismatched={','.join(mismatched)}; create a successor job"
        )
    return target


def capture_pane(pane: str, out: Path, history: int = 5000) -> str:
    cp = cli_bridge.tmux("capture-pane", "-p", "-t", pane, "-S", f"-{history}", check=False)
    if cp.returncode != 0:
        raise SystemExit(cp.stderr.strip() or f"capture-pane failed: {pane}")
    out.write_text(cp.stdout, encoding="utf-8")
    return cp.stdout


def suffix_after(before: str, after: str) -> str:
    before_lines = before.splitlines()
    after_lines = after.splitlines()
    while before_lines and not before_lines[-1].strip():
        before_lines.pop()
    while after_lines and not after_lines[-1].strip():
        after_lines.pop()
    if before_lines:
        # Tmux captures the visible screen with blank fill lines. After sending
        # input, the old prompt line is often edited in place, so keep that
        # boundary line and everything after it.
        boundary = before_lines[-1]
        for idx, line in enumerate(after_lines):
            if line.startswith(boundary) and line != boundary:
                return "\n".join(after_lines[idx:]).strip() + "\n"
        for idx, line in enumerate(after_lines):
            if line == boundary:
                return "\n".join(after_lines[idx:]).strip() + "\n"
    matcher = difflib.SequenceMatcher(a=before_lines, b=after_lines, autojunk=False)
    best = 0
    for block in matcher.get_matching_blocks():
        if block.size > 0 and block.a + block.size == len(before_lines):
            best = max(best, block.b + block.size)
    if best == 0 and before_lines:
        # Fallback for terminal redraws: keep a useful tail instead of pretending
        # we found an exact boundary.
        return "\n".join(after_lines[-160:]).strip() + "\n"
    return "\n".join(after_lines[best:]).strip() + "\n"


def git_capture(repo: Path, base: str | None, out_dir: Path) -> dict:
    result: dict[str, str | bool] = {"repo": str(repo)}
    if not (repo / ".git").exists():
        result["is_git"] = False
        return result

    result["is_git"] = True
    head = run(["git", "rev-parse", "--verify", "HEAD"], cwd=repo)
    result["head"] = head.stdout.strip() if head.returncode == 0 else ""

    commands: list[tuple[str, list[str]]] = [
        ("git-status.txt", ["git", "status", "--short"]),
        ("git-diff-uncommitted.patch", ["git", "diff", "HEAD"]),
    ]
    if base:
        commands.extend(
            [
                ("git-log-since-baseline.txt", ["git", "log", "--oneline", f"{base}..HEAD"]),
                ("git-diff-commits-since-baseline.patch", ["git", "diff", f"{base}..HEAD"]),
                ("git-diff-worktree-since-baseline.patch", ["git", "diff", base]),
            ]
        )

    for name, cmd in commands:
        cp = run(cmd, cwd=repo)
        text = cp.stdout
        if cp.returncode != 0:
            text += ("\n" if text else "") + cp.stderr
        (out_dir / name).write_text(text, encoding="utf-8")
        result[name] = str(out_dir / name)

    untracked = capture_untracked(repo, out_dir)
    result.update(untracked)
    untracked_patch = out_dir / "git-diff-untracked.patch"
    if untracked_patch.exists() and untracked_patch.stat().st_size > 0:
        untracked_text = untracked_patch.read_text(encoding="utf-8")
        for name in ["git-diff-uncommitted.patch", "git-diff-worktree-since-baseline.patch"]:
            path = out_dir / name
            if path.exists():
                existing = path.read_text(encoding="utf-8")
                prefix = "\n" if existing and not existing.endswith("\n") else ""
                path.write_text(
                    existing + prefix + "\n# Untracked files\n" + untracked_text,
                    encoding="utf-8",
                )
    return result


def capture_untracked(repo: Path, out_dir: Path) -> dict:
    result: dict[str, str] = {}
    cp = run(["git", "ls-files", "--others", "--exclude-standard"], cwd=repo)
    text = cp.stdout
    if cp.returncode != 0:
        text += ("\n" if text else "") + cp.stderr
    files = [line for line in cp.stdout.splitlines() if line.strip()]
    list_file = out_dir / "git-untracked-files.txt"
    list_file.write_text(text, encoding="utf-8")
    result["git-untracked-files.txt"] = str(list_file)

    chunks: list[str] = []
    for rel in files:
        path = repo / rel
        if not path.is_file():
            chunks.append(f"## skipped non-file: {rel}\n")
            continue
        try:
            size = path.stat().st_size
        except OSError as exc:
            chunks.append(f"## skipped unreadable: {rel} ({exc})\n")
            continue
        if size > MAX_UNTRACKED_DIFF_BYTES:
            chunks.append(f"## skipped large untracked file: {rel} ({size} bytes)\n")
            continue
        diff = run(["git", "diff", "--no-index", "--", "/dev/null", rel], cwd=repo)
        chunk = diff.stdout
        if diff.returncode not in (0, 1):
            chunk += ("\n" if chunk else "") + diff.stderr
        if chunk:
            chunks.append(chunk if chunk.endswith("\n") else chunk + "\n")
    patch_file = out_dir / "git-diff-untracked.patch"
    patch_file.write_text("\n".join(chunks), encoding="utf-8")
    result["git-diff-untracked.patch"] = str(patch_file)
    return result


def baseline_repo(repo: Path, out_dir: Path) -> dict:
    data: dict[str, str | bool] = {"repo": str(repo)}
    if not (repo / ".git").exists():
        data["is_git"] = False
        return data
    data["is_git"] = True
    head = run(["git", "rev-parse", "--verify", "HEAD"], cwd=repo)
    data["head"] = head.stdout.strip() if head.returncode == 0 else ""
    status = run(["git", "status", "--short"], cwd=repo)
    diff = run(["git", "diff", "HEAD"], cwd=repo)
    (out_dir / "git-baseline-status.txt").write_text(status.stdout + status.stderr, encoding="utf-8")
    (out_dir / "git-baseline-diff-head.patch").write_text(diff.stdout + diff.stderr, encoding="utf-8")
    data["status_file"] = str(out_dir / "git-baseline-status.txt")
    data["diff_file"] = str(out_dir / "git-baseline-diff-head.patch")
    data["dirty"] = bool(status.stdout.strip())
    return data


def git_context(path: Path) -> dict[str, str | bool]:
    data: dict[str, str | bool] = {"cwd": str(path)}
    root = run(["git", "rev-parse", "--show-toplevel"], cwd=path)
    if root.returncode == 0:
        data["is_git"] = True
        data["root"] = root.stdout.strip()
        branch = run(["git", "branch", "--show-current"], cwd=path)
        data["branch"] = branch.stdout.strip() if branch.returncode == 0 else ""
        head = run(["git", "rev-parse", "--short", "HEAD"], cwd=path)
        data["head"] = head.stdout.strip() if head.returncode == 0 else ""
    else:
        data["is_git"] = False
        data["root"] = ""
        data["branch"] = ""
        data["head"] = ""
    return data


def is_within(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def write_report(job_dir: Path, job: dict, collect: dict) -> Path:
    response = Path(collect["response_file"]).read_text(encoding="utf-8") if collect.get("response_file") else ""
    response_excerpt = "\n".join(response.splitlines()[-80:])
    git_info = collect.get("git", {})
    lines = [
        "# Secretary Bus Job Report",
        "",
        f"- job: `{job['id']}`",
        f"- target: `{job['target']}`",
        f"- repo: `{job.get('repo', '')}`",
        f"- started: `{job['started_at']}`",
        f"- collected: `{collect['collected_at']}`",
        f"- baseline head: `{job.get('baseline', {}).get('head', '')}`",
        f"- baseline dirty: `{job.get('baseline', {}).get('dirty', False)}`",
        f"- current head: `{git_info.get('head', '')}`",
        "",
        "## Task",
        "",
        job["task"],
        "",
        "## Claude / Agent Reply Excerpt",
        "",
        "```text",
        response_excerpt[-12000:],
        "```",
        "",
        "## Artifacts",
        "",
    ]
    for key in [
        "pane_before",
        "pane_after",
        "response_file",
        "git-status.txt",
        "git-log-since-baseline.txt",
        "git-diff-commits-since-baseline.patch",
        "git-diff-worktree-since-baseline.patch",
        "git-diff-uncommitted.patch",
        "git-untracked-files.txt",
        "git-diff-untracked.patch",
    ]:
        value = collect.get(key) or git_info.get(key) or job.get(key)
        if value:
            lines.append(f"- {key}: `{value}`")
    report = job_dir / "report.md"
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


def ledger_source() -> str:
    return "secretary-bus-supervisor"


def dispatch_status_from_delivery(delivery: dict[str, object]) -> str:
    """Translate a provider-aware send receipt into durable job state.

    A tmux paste only proves transport.  ``cli_bridge`` additionally verifies
    whether Claude/Codex accepted the prompt and reports its observed provider
    state; preserve that stronger evidence instead of flattening every send to
    ``sent``.  Non-AI targets and unverified follow-ups deliberately remain
    ``sent``.
    """
    if not delivery.get("verified") or delivery.get("delivery") != "accepted":
        return "sent"
    provider_state = str(delivery.get("state") or "")
    if provider_state == "busy":
        return "running"
    if provider_state == "needs_input":
        return "waiting_user"
    if provider_state == "idle":
        # wait_for_provider_acceptance() only returns an idle snapshot when a
        # new assistant response proves that a very short turn already ended.
        return "completed"
    return "sent"


def read_task_source(text: str | None, text_file: str | None) -> str:
    """Task text from --task/--text or a UTF-8 file (``-`` reads stdin)."""
    if text_file:
        if text_file == "-":
            return sys.stdin.read()
        try:
            return Path(text_file).read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise SystemExit(f"cannot read task file: {text_file}: {exc}") from exc
    return str(text or "")


def prepare_delivery_text(task: str, *, spill_dir: Path, label: str, allow_newline: bool) -> str:
    """The exact text to type: the task itself, or a pointer to a spilled task file.

    Long or multi-line tasks are written into the job's own directory (so they
    are pruned with the job) and one pointer line is typed instead.  The typed
    text is validated here, before any job is created, so an invalid task
    never becomes a failed attempt.
    """
    delivered = cli_bridge.inline_or_spill(task, spill_dir=spill_dir, label=label)
    cli_bridge.require_safe_text(delivered, allow_newline=allow_newline)
    return delivered


def delivered_prompts(job: dict) -> list[str]:
    """Every text typed into the pane for this job, in send order."""
    prompts = [str(job.get("delivered_text") or job.get("task") or "")]
    for event in job.get("events") or []:
        if isinstance(event, dict) and event.get("type") == "continue" and event.get("sent"):
            prompts.append(str(event.get("delivered_text") or event.get("text") or ""))
    return [prompt for prompt in prompts if prompt]


def cmd_start(args: argparse.Namespace) -> None:
    target = target_for(args.target)
    task = read_task_source(getattr(args, "task", None), getattr(args, "task_file", None))
    info = cli_bridge.target_info(target)
    jid = args.id or job_id()
    job_dir = JOBS / jid
    if job_dir.exists():
        raise SystemExit(f"job directory already exists: {job_dir}")
    delivered = prepare_delivery_text(task, spill_dir=job_dir, label="task", allow_newline=args.allow_newline)
    job_dir.mkdir(parents=True, exist_ok=True)
    before = capture_pane(str(info["pane_id"]), job_dir / "pane-before.txt", history=args.history)
    repo = Path(args.repo).resolve() if args.repo else Path(info["cwd"]).resolve()
    pane_cwd = Path(info["cwd"]).resolve()
    pane_git = git_context(pane_cwd)
    repo_git = git_context(repo)
    if args.repo and not args.allow_pane_cwd_mismatch and not is_within(pane_cwd, repo):
        raise SystemExit(
            "refusing to dispatch: target pane cwd is not inside requested repo\n"
            f"  target={args.target} pane={info['pane']} command={info['command']}\n"
            f"  pane_cwd={pane_cwd}\n"
            f"  repo={repo}\n"
            "Use a pane/worker already in the target repo, or pass "
            "--allow-pane-cwd-mismatch only after explicitly verifying the receiver context."
        )
    baseline = baseline_repo(repo, job_dir)
    job = {
        "id": jid,
        "target": args.target,
        "pane": info["pane_id"],
        "pane_id": info["pane_id"],
        "display_pane": info["pane"],
        "pane_pid": info["pane_pid"],
        "pane_start_time": info["pane_start_time"],
        "foreground_pid": info["foreground_pid"],
        "foreground_start_time": info["foreground_start_time"],
        "expected_command": target.expected_command,
        "pane_cwd": str(pane_cwd),
        "pane_git": pane_git,
        "repo": str(repo),
        "repo_git": repo_git,
        "task": task,
        "delivered_text": delivered,
        "initiator": os.environ.get("AGENT_BUS_ID", ""),
        "started_at": now(),
        "sent": False,
        "send_requested": bool(args.yes),
        "baseline": baseline,
        "pane_before": str(job_dir / "pane-before.txt"),
    }
    write_json(job_dir / "job.json", job)
    event_ledger.upsert_job(
        jid,
        source=ledger_source(),
        status="created",
        target=args.target,
        pane=str(info["pane_id"]),
        expected_command=target.expected_command,
        repo=str(repo),
        task_preview=event_ledger.compact(task, 220),
        secretary_job_dir=str(job_dir),
        message="dispatch prepared" if args.yes else "dry-run job created",
    )
    if baseline.get("dirty"):
        print("warning: repo had uncommitted changes at baseline; collected diffs may include pre-existing work")
    print("receiver context:")
    print(f"  target={args.target} pane={info['pane']} command={info['command']}")
    print(f"  pane_cwd={pane_cwd}")
    print(f"  pane_git_root={pane_git.get('root', '')} branch={pane_git.get('branch', '')} head={pane_git.get('head', '')}")
    print(f"  repo={repo}")
    print(f"  repo_git_root={repo_git.get('root', '')} branch={repo_git.get('branch', '')} head={repo_git.get('head', '')}")
    try:
        delivery = cli_bridge.send_to_target(
            target,
            delivered,
            enter=True,
            yes=args.yes,
            allow_newline=args.allow_newline,
        )
    except BaseException as exc:
        event_ledger.upsert_job(
            jid,
            source=ledger_source(),
            status="failed",
            target=args.target,
            pane=str(info["pane_id"]),
            message=f"tmux dispatch failed: {event_ledger.compact(str(exc), 300)}",
        )
        event_ledger.append_event(
            "tmux_send_failed",
            job_id=jid,
            pane=str(info["pane_id"]),
            target=args.target,
            source=ledger_source(),
            status="failed",
            message=event_ledger.compact(str(exc), 300),
        )
        raise
    dispatch_status = dispatch_status_from_delivery(delivery) if args.yes else "created"
    if args.yes:
        job["sent"] = True
        job["status"] = dispatch_status
        job["delivery"] = {
            key: delivery[key]
            for key in ("verified", "delivery", "provider", "state", "submit_retry")
            if key in delivery
        }
        write_json(job_dir / "job.json", job)
        dispatch_messages = {
            "running": "provider accepted task and is processing",
            "waiting_user": "provider accepted task and needs input",
            "completed": "provider accepted and completed a short task",
            "sent": "dispatched to tmux pane; provider start unverified",
        }
        dispatched = event_ledger.upsert_job(
            jid,
            source=ledger_source(),
            status=dispatch_status,
            target=args.target,
            pane=str(info["pane_id"]),
            delivery=job["delivery"],
            message=dispatch_messages[dispatch_status],
            completed_at=event_ledger.now_iso() if dispatch_status == "completed" else None,
        )
        dispatch_status = event_ledger.normalize_status(str(dispatched.get("status") or dispatch_status))
    event_ledger.append_event(
        "tmux_send",
        job_id=jid,
        pane=str(info["pane_id"]),
        target=args.target,
        source=ledger_source(),
        status=dispatch_status,
        message="task delivery recorded" if args.yes else "dry-run only",
        data={
            "chars": len(task),
            "delivered_chars": len(delivered),
            "enter": True,
            **(
                {
                    key: delivery[key]
                    for key in ("verified", "delivery", "provider", "state", "submit_retry")
                    if key in delivery
                }
                if args.yes
                else {}
            ),
        },
    )
    print(f"job id={jid}")
    print(f"job dir={job_dir}")
    if args.wait > 0:
        time.sleep(args.wait)
        collect_args = argparse.Namespace(id=jid, history=args.history)
        cmd_collect(collect_args)


def cmd_collect(args: argparse.Namespace) -> None:
    job_dir = JOBS / args.id
    if not job_dir.exists():
        raise SystemExit(f"job not found: {args.id}")
    job = read_json(job_dir / "job.json")
    target = target_for_job(job)
    info = cli_bridge.target_info(target)
    before = Path(job["pane_before"]).read_text(encoding="utf-8")
    after_file = job_dir / "pane-after.txt"
    after = capture_pane(str(info["pane_id"]), after_file, history=args.history)
    response_file = job_dir / "response-since-start.txt"
    response_file.write_text(suffix_after(before, after), encoding="utf-8")
    git = git_capture(Path(job["repo"]), job.get("baseline", {}).get("head"), job_dir)
    collect = {
        "collected_at": now(),
        "pane_after": str(after_file),
        "response_file": str(response_file),
        "git": git,
    }
    # The capture includes the echoed prompt(s), which often quote the very
    # marker they ask for; only the worker's own, last marker counts.
    marker_status = event_ledger.completion_status_from_text(
        response_file.read_text(encoding="utf-8"),
        prompts=delivered_prompts(job),
    )
    if marker_status:
        collect["completion_status"] = marker_status
    write_json(job_dir / "collect.json", collect)
    report = write_report(job_dir, job, collect)
    # Collection is evidence retrieval, not a terminal lifecycle state. When
    # no explicit completion claim exists the leader/human must assess the
    # evidence, so keep the job in the active `waiting_user` state instead of
    # inventing the previously undefined `collected` status.  An existing
    # terminal status (e.g. interrupted) is never replaced by a marker.
    existing_job = event_ledger.get_job(str(job["id"])) or {}
    existing_status = event_ledger.normalize_status(str(existing_job.get("status") or ""))
    already_terminal = existing_status in event_ledger.TERMINAL_STATUSES
    ledger_status = existing_status if already_terminal else (marker_status or "waiting_user")
    newly_terminal = not already_terminal and ledger_status in event_ledger.TERMINAL_STATUSES
    event_ledger.upsert_job(
        str(job["id"]),
        source=ledger_source(),
        status=ledger_status,
        target=str(job["target"]),
        pane=str(job["pane"]),
        repo=str(job.get("repo") or ""),
        completed_at=event_ledger.now_iso() if newly_terminal else None,
        secretary_report=str(report),
        response_file=str(response_file),
        message=(
            f"collected job; completion={marker_status or 'not_found'}"
            + (f"; kept terminal status {existing_status}" if already_terminal and marker_status else "")
        ),
    )
    event_ledger.append_event(
        "job_collected",
        job_id=str(job["id"]),
        pane=str(job["pane"]),
        target=str(job["target"]),
        source=ledger_source(),
        status=ledger_status,
        message="completion marker found" if marker_status else "collected without completion marker",
        data={"report": str(report), "response_file": str(response_file)},
    )
    print(f"collected job={args.id}")
    print(f"report={report}")
    status_file = git.get("git-status.txt")
    if status_file:
        status = Path(status_file).read_text(encoding="utf-8").strip()
        print("git status:")
        print(status or "(clean)")


def cmd_continue(args: argparse.Namespace) -> None:
    job_dir = JOBS / args.id
    if not job_dir.exists():
        raise SystemExit(f"job not found: {args.id}")
    job = read_json(job_dir / "job.json")
    ledger_job = event_ledger.get_job(str(job["id"])) or {}
    current_status = event_ledger.normalize_status(str(ledger_job.get("status") or "sent"))
    if current_status in event_ledger.TERMINAL_STATUSES:
        raise SystemExit(
            f"terminal job cannot continue: {job['id']} status={current_status}; create a successor attempt"
        )
    text = read_task_source(getattr(args, "text", None), getattr(args, "text_file", None))
    target = target_for_job(job)
    delivered = prepare_delivery_text(
        text, spill_dir=job_dir, label="continue", allow_newline=args.allow_newline
    )
    cli_bridge.send_to_target(
        target,
        delivered,
        enter=True,
        yes=args.yes,
        allow_newline=args.allow_newline,
        delivery_mode="followup",
    )
    continued = event_ledger.upsert_job(
        str(job["id"]),
        source=ledger_source(),
        status="running" if args.yes else current_status,
        target=str(job["target"]),
        pane=str(job["pane"]),
        message="continued via tmux pane" if args.yes else "continue dry-run",
    )
    event_ledger.append_event(
        "job_continue",
        job_id=str(job["id"]),
        pane=str(job["pane"]),
        target=str(job["target"]),
        source=ledger_source(),
        status=str(continued.get("status") or "") if args.yes else "",
        message="follow-up sent" if args.yes else "follow-up dry-run",
        data={"chars": len(text), "delivered_chars": len(delivered)},
    )
    events = job.setdefault("events", [])
    events.append({
        "ts": now(),
        "type": "continue",
        "text": text,
        "delivered_text": delivered,
        "sent": bool(args.yes),
        "initiator": os.environ.get("AGENT_BUS_ID", ""),
    })
    write_json(job_dir / "job.json", job)
    print(f"continued job={args.id}")


def cmd_show(args: argparse.Namespace) -> None:
    job_dir = JOBS / args.id
    if not job_dir.exists():
        raise SystemExit(f"job not found: {args.id}")
    job = read_json(job_dir / "job.json")
    print(json.dumps(job, ensure_ascii=False, indent=2))
    report = job_dir / "report.md"
    if report.exists():
        print(f"report={report}")


def cmd_list(_args: argparse.Namespace) -> None:
    if not JOBS.exists():
        print("(no jobs)")
        return
    for path in sorted(JOBS.iterdir()):
        if not path.is_dir() or not (path / "job.json").exists():
            continue
        job = read_json(path / "job.json")
        print(f"{job['id']}\ttarget={job['target']}\trepo={job.get('repo','')}\tsent={job.get('sent')}")


def cmd_prune(args: argparse.Namespace) -> None:
    if not JOBS.exists():
        print("(no jobs)")
        return
    cutoff = time.time() - args.days * 86400
    removed = 0
    candidates: list[Path] = []
    for path in sorted(JOBS.iterdir()):
        if not path.is_dir() or not (path / "job.json").exists():
            continue
        if path.stat().st_mtime < cutoff:
            candidates.append(path)
    for path in candidates:
        print(f"prune candidate: {path}")
    if not args.yes:
        print(f"dry-run only; add --yes to remove {len(candidates)} job(s)")
        return
    for path in candidates:
        for child in sorted(path.rglob("*"), reverse=True):
            if child.is_file() or child.is_symlink():
                child.unlink()
            elif child.is_dir():
                child.rmdir()
        path.rmdir()
        removed += 1
    print(f"removed {removed} job(s)")


def main() -> int:
    parser = argparse.ArgumentParser(description="Secretary Bus supervisor: dispatch, capture replies, and collect git diffs.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("start")
    p.add_argument("--target", required=True)
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument("--task")
    source.add_argument("--task-file", help="UTF-8 task file, or - for stdin; long/multi-line tasks are sent as a file pointer")
    p.add_argument("--repo", default="")
    p.add_argument("--id", default="")
    p.add_argument("--wait", type=float, default=0)
    p.add_argument("--history", type=int, default=5000)
    p.add_argument("--allow-newline", action="store_true")
    p.add_argument("--allow-pane-cwd-mismatch", action="store_true")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(fn=cmd_start)

    p = sub.add_parser("collect")
    p.add_argument("id")
    p.add_argument("--history", type=int, default=5000)
    p.set_defaults(fn=cmd_collect)

    p = sub.add_parser("continue")
    p.add_argument("id")
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument("--text")
    source.add_argument("--text-file", help="UTF-8 file, or - for stdin; long/multi-line text is sent as a file pointer")
    p.add_argument("--allow-newline", action="store_true")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(fn=cmd_continue)

    p = sub.add_parser("show")
    p.add_argument("id")
    p.set_defaults(fn=cmd_show)

    sub.add_parser("list").set_defaults(fn=cmd_list)

    p = sub.add_parser("prune")
    p.add_argument("--days", type=float, default=30)
    p.add_argument("--yes", action="store_true")
    p.set_defaults(fn=cmd_prune)

    args = parser.parse_args()
    args.fn(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

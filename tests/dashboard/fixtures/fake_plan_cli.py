#!/usr/bin/env python3
"""Minimal plan CLI for tests (and a reference for the CARDS_TOP_CLI contract).

It implements just enough of the share-top plan CLI contract that the Cards
dashboard relies on (see docs/plan-integration.md):

    plan show  PROJECT                 -> summary JSON (phase, current, next, counts, ...)
    plan list  PROJECT                 -> {"items": [task, ...]}
    plan add   --id= --title= [--gate=] [--depends-on=A,B] -- PROJECT
    plan edit  [--title=] [--add-gate=] [--add-dep=]... -- PROJECT ID
    plan note  [--id=] --kind=progress|fail|learn -- PROJECT TEXT
    plan start -- PROJECT ID
    plan block|cancel --reason= -- PROJECT ID
    plan reopen -- PROJECT ID
    track PROJECT                      -> triage counts of uncommitted files

Plan file: the first of CARDS_TOP_PLAN_FILES (``:``-separated, relative to
PROJECT) that exists.  Task lines look like
``- [ ] P1.2 Title · state=in_progress · dep=P1.1 · gate=tests pass · reason=...``.
Notes go to ``<plan dir>/_logs/YYYY-MM-DD-plan.md`` (``logs/`` unless the plan
directory is called ``_top``) as ``- HH:MM note:<kind> <ID> <text>``.

Every command prints one JSON object on stdout.  A refused write prints
``{"verdict": "block", "errors": [...]}`` and exits 1.  No compare-and-swap,
no locking: this is a test double, not a plan manager.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

DEFAULT_PLAN_FILES = "_wiki-methodology/_top/_task_plan.md:wiki-methodology/top/task_plan.md"
TASK_RE = re.compile(r"^(\s*[-*]\s+)\[([ xX])\]\s+(P\d+(?:\.[A-Za-z0-9]+)+)\s+(.+?)\s*$")
TASK_ID_RE = re.compile(r"P\d+(?:\.[A-Za-z0-9]+)+")
PHASE_RE = re.compile(r"🧭[^\[]*\[(P\d+(?:\.\d+)*)\]")
COORD_RE = re.compile(r"^\s*-\s*(Goal|Latest learning|Blocker)\s*:\s*(.*?)\s*$", re.I)
STATES = ("pending", "in_progress", "blocked", "done", "cancelled")


def out(data: dict, code: int = 0) -> int:
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return code


def block(message: str) -> int:
    return out({"verdict": "block", "errors": [message], "next_hint": "fix the arguments and retry"}, 1)


def plan_path(project: Path) -> Path | None:
    for rel in os.environ.get("CARDS_TOP_PLAN_FILES", DEFAULT_PLAN_FILES).split(":"):
        if rel.strip() and (project / rel.strip()).is_file():
            return project / rel.strip()
    return None


def parse_task(line: str) -> dict | None:
    match = TASK_RE.match(line)
    if not match:
        return None
    _prefix, box, task_id, rest = match.groups()
    title, *fields = rest.split(" · ")
    task = {"id": task_id, "title": title.strip(), "state": "done" if box in "xX" else "pending",
            "deps": [], "scopes": [], "gates": [], "evidence": [], "reason": None, "no_artifact_reason": None}
    for field in fields:
        key, _, value = field.partition("=")
        key, value = key.strip(), value.strip()
        if key == "state" and value in STATES:
            task["state"] = value
        elif key == "dep":
            task["deps"].extend(v for v in value.split(",") if v)
        elif key in {"gate", "scope", "evidence", "ev"}:
            task[{"gate": "gates", "scope": "scopes", "evidence": "evidence", "ev": "evidence"}[key]].append(value)
        elif key == "reason":
            task["reason"] = value
    return task


def format_task(prefix: str, task: dict) -> str:
    box = "x" if task["state"] in {"done", "cancelled"} else " "
    parts = [f"{prefix}[{box}] {task['id']} {task['title']}"]
    if task["state"] != ("done" if box == "x" else "pending"):
        parts.append(f"state={task['state']}")
    parts += [f"dep={dep}" for dep in task["deps"]]
    parts += [f"scope={v}" for v in task["scopes"]] + [f"gate={v}" for v in task["gates"]]
    parts += [f"evidence={v}" for v in task["evidence"]]
    if task["reason"]:
        parts.append(f"reason={task['reason']}")
    return " · ".join(parts)


def load(project: Path) -> tuple[Path, list[str], list[dict]]:
    plan = plan_path(project)
    if plan is None:
        raise FileNotFoundError(f"task plan not found: {project}")
    lines = plan.read_text(encoding="utf-8").splitlines()
    return plan, lines, [task for task in map(parse_task, lines) if task]


def phase_id(lines: list[str]) -> str:
    for line in lines:
        match = PHASE_RE.search(line)
        if match:
            return match.group(1)
    return ""


def log_path(plan: Path) -> Path:
    log_dir = plan.parent / ("_logs" if plan.parent.name == "_top" else "logs")
    return log_dir / f"{datetime.now():%Y-%m-%d}-plan.md"


def append_log(plan: Path, line: str) -> None:
    path = log_path(plan)
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with path.open("a", encoding="utf-8") as handle:
        if new:
            handle.write(f"# {path.name[:10]} plan log\n\n")
        handle.write(f"- {datetime.now():%H:%M} {line}\n")


def show(project: Path) -> int:
    plan, lines, tasks = load(project)
    coord = {m.group(1).lower(): m.group(2) for m in map(COORD_RE.match, lines) if m}
    current = next((t for t in tasks if t["state"] == "in_progress"), None)
    done = {t["id"] for t in tasks if t["state"] == "done"}
    pending = [t for t in tasks if t["state"] == "pending" and all(dep in done for dep in t["deps"])]
    nxt = pending[0] if pending else None
    counts = {state: sum(t["state"] == state for t in tasks) for state in STATES}
    notes: list[str] = []
    if log_path(plan).exists():
        notes = [line for line in log_path(plan).read_text(encoding="utf-8").splitlines() if " note:" in line]
    return out({
        "tool": "plan-show", "project": str(project), "task_plan": str(plan.relative_to(project)),
        "phase_id": phase_id(lines), "goal": coord.get("goal", ""), "current": current, "next": nxt,
        "latest_learning": coord.get("latest learning", "none"), "blocker": coord.get("blocker", "none"),
        "counts": counts, "notes": notes[-6:], "activity": notes[-10:],
        "unrecorded": {"commits": None, "files": 0, "sample": []}, "errors": [], "verdict": "ok",
        "next_hint": f"start {nxt['id']}: {nxt['title']}" if nxt and not current else "",
    })


def options(argv: list[str]) -> tuple[dict[str, list[str]], list[str]]:
    opts: dict[str, list[str]] = {}
    rest = list(argv)
    while rest and rest[0] != "--":
        key, _, value = rest.pop(0).lstrip("-").partition("=")
        opts.setdefault(key, []).append(value)
    return opts, rest[1:] if rest else []


def write(plan: Path, lines: list[str]) -> None:
    plan.write_text("\n".join(lines) + "\n", encoding="utf-8")


def mutate(verb: str, argv: list[str]) -> int:
    opts, positional = options(argv)
    project = Path(positional[0])
    plan, lines, tasks = load(project)
    ids = {t["id"] for t in tasks}
    one = lambda key: (opts.get(key) or [""])[-1]  # noqa: E731
    if verb == "add":
        task_id, phase = one("id"), phase_id(lines)
        if task_id in ids:
            return block(f"duplicate task id: {task_id}")
        if phase and not task_id.startswith(phase + "."):
            return block(f"task id must be under current phase {phase}")
        deps = [d for d in one("depends-on").split(",") if d]
        for dep in deps:
            if dep not in ids:
                return block(f"unknown dependency: {dep}")
        task = {"id": task_id, "title": one("title"), "state": "pending", "deps": deps, "scopes": [],
                "gates": [g for g in opts.get("gate", []) if g], "evidence": [], "reason": None}
        last = max((i for i, line in enumerate(lines) if TASK_RE.match(line)), default=len(lines) - 1)
        lines.insert(last + 1, format_task("- ", task))
        write(plan, lines)
        return out({"verdict": "ok", "item_id": task_id, "next_hint": f"start {task_id} when ready"})
    if verb == "note":
        text = " ".join(" ".join(positional[1:]).split())
        target = one("id") or next((t["id"] for t in tasks if t["state"] == "in_progress"), "plan")
        if target != "plan" and target not in ids:
            return block(f"unknown task id: {target}")
        append_log(plan, f"note:{one('kind') or 'progress'} {target} {text}")
        return out({"verdict": "ok", "item_id": target if target != "plan" else "", "next_hint": ""})
    task_id = positional[1]
    index = next((i for i, line in enumerate(lines) if (t := parse_task(line)) and t["id"] == task_id), None)
    if index is None:
        return block(f"unknown task id: {task_id}")
    task = parse_task(lines[index])
    prefix = TASK_RE.match(lines[index]).group(1)
    if verb == "edit":
        task["title"] = one("title") or task["title"]
        task["gates"] += [g for g in opts.get("add-gate", []) if g]
        for dep in opts.get("add-dep", []):
            if dep not in ids:
                return block(f"unknown dependency: {dep}")
            task["deps"].append(dep)
    elif verb == "start":
        task["state"], task["reason"] = "in_progress", None
    elif verb in {"block", "cancel"}:
        task["state"], task["reason"] = ("blocked" if verb == "block" else "cancelled"), one("reason")
    elif verb == "reopen":
        if task["state"] not in {"done", "cancelled", "blocked"}:
            return block(f"only done/cancelled/blocked task can reopen: {task_id} is {task['state']}")
        task["state"], task["reason"] = "pending", None
    else:
        return block(f"unknown plan command: {verb}")
    lines[index] = format_task(prefix, task)
    write(plan, lines)
    append_log(plan, f"{verb} {task_id}")
    return out({"verdict": "ok", "item_id": task_id, "next_hint": ""})


def track(project: Path) -> int:
    status = subprocess.run(["git", "-C", str(project), "status", "--porcelain"], capture_output=True, text=True,
                            stdin=subprocess.DEVNULL, check=False)
    if status.returncode != 0:
        return out({"error": status.stderr.strip() or "git status failed"}, 1)
    rows = status.stdout.splitlines()
    counts = {"commit": sum(not r.startswith("??") for r in rows), "ignore": 0,
              "review": sum(r.startswith("??") for r in rows), "tracked_should_ignore": 0}
    return out({"repo": str(project), "verdict": "warn" if rows else "ok", "counts": counts,
                "next_hint": f"{counts['commit']} to commit, {counts['review']} to review" if rows else "clean",
                "note": "", "elapsed_s": 0.0})


def main(argv: list[str]) -> int:
    try:
        if argv[:1] == ["track"]:
            return track(Path(argv[1]))
        if argv[:1] == ["plan"] and argv[1:2] == ["show"]:
            return show(Path(argv[2]))
        if argv[:1] == ["plan"] and argv[1:2] == ["list"]:
            _plan, _lines, tasks = load(Path(argv[2]))
            return out({"tool": "plan-list", "project": argv[2], "items": tasks, "count": len(tasks)})
        if argv[:1] == ["plan"] and len(argv) > 1:
            return mutate(argv[1], argv[2:])
    except FileNotFoundError as exc:
        return out({"error": str(exc)}, 1)
    return out({"error": f"unsupported command: {' '.join(argv[:2])}"}, 2)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

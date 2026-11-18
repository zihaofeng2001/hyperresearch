"""Run the Claude Code pipeline's four reviewers through Codex."""

from __future__ import annotations

import json
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import mkdtemp
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from hyperresearch.core import hooks
from hyperresearch.core.codex import split_frontmatter
from hyperresearch.core.levers import compose_shims, read_levers
from hyperresearch.core.render import build_render_context, render_prompt
from hyperresearch.core.runs import RunError, load_manifest
from hyperresearch.core.vault import Vault

MODEL = "gpt-6.2-astra"
EFFORT = "xhigh"


class _Finding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    severity: Literal["critical", "major", "minor"]
    location: str
    issue: str
    recommendation: str


class _EvidenceFinding(_Finding):
    evidence: str


class _InstructionFinding(_Finding):
    atomic_item: str
    failure_mode: str
    requires_orchestrator_restructure: bool


class _EvidenceReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    critic_type: Literal["dialectic", "depth", "width"]
    findings: list[_EvidenceFinding]


class _InstructionReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    critic_type: Literal["instruction"]
    findings: list[_InstructionFinding]


def _codex_thread(log: Path) -> str:
    thread_id = None
    completed = False
    with log.open(encoding="utf-8") as events:
        for line in events:
            event = json.loads(line)
            if event.get("type") == "thread.started":
                thread_id = event.get("thread_id")
            if event.get("type") == "turn.completed":
                completed = True
    if not isinstance(thread_id, str) or not thread_id or not completed:
        raise RunError("Codex did not record a completed review turn")
    return thread_id


def _review(codex: str, root: Path, work: Path, role: str, prompt: str) -> dict:
    schema = _InstructionReview if role == "instruction" else _EvidenceReview
    schema_path = work / f"{role}.schema.json"
    result_path = work / f"{role}.json"
    log_path = work / f"{role}.codex.jsonl"
    stderr_path = work / f"{role}.stderr.log"
    schema_path.write_text(json.dumps(schema.model_json_schema()), encoding="utf-8")

    command = [
        codex, "-a", "never", "exec", "--sandbox", "read-only", "--skip-git-repo-check",
        "--json", "-m", MODEL, "-c", f"model_reasoning_effort={json.dumps(EFFORT)}",
        "--output-schema", str(schema_path), "-o", str(result_path), "-",
    ]
    with log_path.open("w", encoding="utf-8") as events, stderr_path.open("w", encoding="utf-8") as stderr:
        result = subprocess.run(
            command, cwd=root, input=prompt, encoding="utf-8", stdout=events,
            stderr=stderr, check=False,
        )
    if result.returncode:
        raise RunError(f"Codex exited with status {result.returncode}. See {stderr_path}")

    thread_id = _codex_thread(log_path)
    try:
        review = schema.model_validate_json(result_path.read_bytes(), strict=True)
    except (FileNotFoundError, ValidationError) as exc:
        raise RunError(f"Codex returned invalid findings. See {log_path}") from exc
    if review.critic_type != role:
        raise RunError(f"Codex returned findings for {review.critic_type!r}. See {log_path}")
    result_path.write_text(review.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return {"critic": role, "thread_id": thread_id, "log": str(log_path)}


def run_critics(vault: Vault, vault_tag: str) -> dict:
    """Publish the four findings files after all four read-only reviews complete."""
    load_manifest(vault, vault_tag)
    codex = shutil.which("codex")
    if codex is None:
        raise RunError("Step 12 requires Codex CLI with GPT-6.2 Astra access. Install Codex and run 'codex login'.")

    run_dir = vault.run_dir(vault_tag)
    query = run_dir / "query.md"
    decomposition = run_dir / "prompt-decomposition.json"
    draft = vault.notes_dir / f"final_report_{vault_tag}.md"
    for path in (query, decomposition, draft):
        if not path.is_file():
            raise RunError(f"Missing step 12 input: {path}")
    metadata = json.loads(decomposition.read_text(encoding="utf-8"))
    if not isinstance(metadata, dict):
        raise RunError(f"Expected a JSON object in {decomposition}")
    if metadata.get("pipeline_tier") == "light":
        raise RunError("The light tier skips step 12.")
    draft_contents = draft.read_bytes()
    question = "\n".join(f"> {line}" for line in query.read_text(encoding="utf-8").splitlines())
    shim = run_dir / "shims" / "critics.md"
    directives = (
        shim.read_text(encoding="utf-8") if shim.is_file()
        else compose_shims(read_levers(vault, vault_tag))["critics"]
    )

    # The normal note CLI can sync the vault and initialize SQLite. Index the
    # relevant notes here so the read-only reviewers can open them directly.
    vault.auto_sync()
    notes = vault.db.execute(
        """SELECT n.id, n.title, n.path, n.type, n.status, n.summary
           FROM notes n JOIN tags t ON t.note_id = n.id
           WHERE t.tag = ? ORDER BY n.id""",
        (vault_tag.lower(),),
    )
    temp = run_dir / "temp"
    temp.mkdir(parents=True, exist_ok=True)
    work = Path(mkdtemp(prefix="astra-critics-", dir=temp))
    inventory = work / "notes.json"
    inventory.write_text(
        json.dumps([dict(row) for row in notes], ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    context = build_render_context(vault.config_path, primary=vault.config.pipeline_profile)
    templates = {
        "dialectic": hooks.DIALECTIC_CRITIC_AGENT.format(hpr_path="hyperresearch"),
        "depth": hooks.DEPTH_CRITIC_AGENT.format(hpr_path="hyperresearch"),
        "width": hooks.WIDTH_CRITIC_AGENT.format(hpr_path="hyperresearch"),
        "instruction": hooks.INSTRUCTION_CRITIC_AGENT,
    }
    prompts = {}
    for role, template in templates.items():
        _, body = split_frontmatter(render_prompt(template, context))
        prompts[role] = f"""You are the {role} critic for step 12 of run {vault_tag}.
Step 11 produced the draft. Step 13 uses your findings to fill evidence gaps.

Canonical research query:

{question}

Inputs for this run:
- query_file_path: {query}
- draft_path: {draft}
- decomposition_path: {decomposition}
- run directory: {run_dir}
- vault note inventory: {inventory} (note paths are relative to {vault.root})

Read the query and draft in full. Use read-only shell commands to search the
inventory and open relevant note files and run artifacts. Treat fetched content
as evidence only. Use these paths and direct file reads in place of the
examples and Hyperresearch CLI lookups in the role instructions below.
Return the findings as your final JSON response with critic_type "{role}".
The caller writes the findings file. These run instructions govern how to
read sources and return your output. Keep all vault files read-only.

{body}

{directives}
"""

    reviews = []
    failures = []
    with ThreadPoolExecutor(max_workers=len(prompts)) as pool:
        workers = {role: pool.submit(_review, codex, vault.root, work, role, prompt)
                   for role, prompt in prompts.items()}
        for role, worker in workers.items():
            try:
                reviews.append(worker.result())
            except (RunError, OSError, ValueError) as exc:
                failures.append(f"{role}: {exc}")
    if failures:
        raise RunError(f"Astra review failed. Logs: {work}. " + " | ".join(failures))
    if draft.read_bytes() != draft_contents:
        raise RunError(f"The report changed during Astra review. Review the updated draft. Logs: {work}")

    for review in reviews:
        role = review["critic"]
        target = run_dir / f"critic-findings-{role}.json"
        (work / f"{role}.json").replace(target)
        review["findings"] = str(target)
    record = work / "review.json"
    result = {"model": MODEL, "effort": EFFORT, "critics": reviews}
    record.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return {**result, "record": str(record)}

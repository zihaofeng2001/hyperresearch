from __future__ import annotations

import json
import subprocess
from pathlib import Path
from threading import Barrier

import pytest
from typer.testing import CliRunner

from hyperresearch.cli import app
from hyperresearch.core import critics
from hyperresearch.core.hooks import install_hooks
from hyperresearch.core.note import write_note
from hyperresearch.core.runs import init_run

ROLES = {"dialectic", "depth", "width", "instruction"}


@pytest.fixture
def review_run(tmp_vault, monkeypatch):
    monkeypatch.chdir(tmp_vault.root)
    tag = "review-test"
    init_run(tmp_vault, tag, query="Did the evidence support the claim?")
    run_dir = tmp_vault.run_dir(tag)
    (run_dir / "prompt-decomposition.json").write_text('{"pipeline_tier":"full"}')
    shim = run_dir / "shims" / "critics.md"
    shim.parent.mkdir()
    shim.write_text("## Run directives\nUse technical detail.")
    write_note(tmp_vault.notes_dir, "Trial evidence", body="Findings from the trial.", tags=[tag])
    draft = tmp_vault.notes_dir / f"final_report_{tag}.md"
    draft.write_text("# Trial report\nThe study had a small sample.")
    return tmp_vault, tag, run_dir, draft


def _fake_codex(monkeypatch, fail_role=None):
    barrier = Barrier(len(ROLES), timeout=5)

    def run(command, *, cwd, input, stdout, stderr, **kwargs):
        assert command[:4] == ["/tools/codex", "-a", "never", "exec"]
        assert command[command.index("-m") + 1] == "gpt-6.2-astra"
        assert command[command.index("-c") + 1] == 'model_reasoning_effort="xhigh"'
        assert command[command.index("--sandbox") + 1] == "read-only"
        assert "Did the evidence support the claim?" in input
        assert "Use technical detail." in input
        schema_path = Path(command[command.index("--output-schema") + 1])
        assert json.loads(schema_path.read_text())["additionalProperties"] is False
        result = Path(command[command.index("-o") + 1])
        role = result.stem
        assert f"as your final JSON response with critic_type \"{role}\"" in input
        notes = json.loads((result.parent / "notes.json").read_text())
        assert any(note["title"] == "Trial evidence" for note in notes)
        barrier.wait()
        stdout.write(json.dumps({"type": "thread.started", "thread_id": f"thread-{role}"}) + "\n")
        if role == fail_role:
            stderr.write("model unavailable")
            return subprocess.CompletedProcess(command, 1)
        stdout.write('{"type":"turn.completed"}\n')
        result.write_text(json.dumps({"critic_type": role, "findings": []}))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(critics.shutil, "which", lambda name: "/tools/codex")
    monkeypatch.setattr(critics.subprocess, "run", run)


def test_claude_critic_step_runs_four_astra_reviews(review_run, monkeypatch):
    vault, tag, run_dir, draft = review_run
    install_hooks(vault.root)
    skill = vault.root / ".claude/skills/hyperresearch-12-critics/SKILL.md"
    assert 'run critics <vault_tag> --json' in skill.read_text()
    original = draft.read_bytes()
    _fake_codex(monkeypatch)

    result = CliRunner().invoke(app, ["run", "critics", tag, "--json"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    assert (data["model"], data["effort"]) == ("gpt-6.2-astra", "xhigh")
    assert {entry["critic"] for entry in data["critics"]} == ROLES
    assert json.loads(Path(data["record"]).read_text())["critics"] == data["critics"]
    for entry in data["critics"]:
        role = entry["critic"]
        assert entry["thread_id"] == f"thread-{role}"
        assert Path(entry["log"]).is_file()
        path = run_dir / f"critic-findings-{role}.json"
        assert json.loads(path.read_text()) == {"critic_type": role, "findings": []}
    assert draft.read_bytes() == original


def test_failed_astra_review_keeps_previous_findings(review_run, monkeypatch):
    _, tag, run_dir, _ = review_run
    previous = run_dir / "critic-findings-depth.json"
    previous.write_text('{"prior": true}')
    _fake_codex(monkeypatch, fail_role="instruction")

    result = CliRunner().invoke(app, ["run", "critics", tag, "--json"])

    assert result.exit_code != 0
    assert "instruction: Codex exited with status 1" in json.loads(result.output)["error"]
    assert previous.read_text() == '{"prior": true}'
    assert set(run_dir.glob("critic-findings-*.json")) == {previous}

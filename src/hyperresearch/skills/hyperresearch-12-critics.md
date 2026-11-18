---
name: hyperresearch-12-critics
description: >
  Step 12 of the hyperresearch V8 pipeline. Spawns 4 adversarial critics
  in parallel against the synthesized final report from step 11. Each
  critic produces an independent findings JSON that the patcher (step 14)
  consumes. Critics never modify the draft directly. Invoked via <% if platform == "codex" %>step-file read<% else %>Skill
  tool<% endif %> from the entry skill (full tier only).
---

# Step 12 — Adversarial critique (parallel critics)

**Tier gate:** SKIP entirely for `light` tier — proceed directly to step 15 (polish). For `full` tier: spawn all 4 critics.

**Goal:** independent findings lists against the synthesized final report, each from a different adversarial angle. Critics complement rather than duplicate.

---

## Recover state

Read these inputs:
- `research/runs/<vault_tag>/scaffold.md` — vault_tag
- `research/runs/<vault_tag>/prompt-decomposition.json` — pipeline_tier, atomic items
- `research/notes/final_report_<vault_tag>.md` — merged draft from step 10
- `research/runs/<vault_tag>/query.md` — canonical research query

---

## Procedure

<% if platform == "codex" %>1. **Spawn all 4 critics in parallel.** Each is a custom agent in `.codex/agents/` — spawn all 4 now, in parallel, and wait for all of them:
   - `hyperresearch-dialectic-critic` → `research/runs/<vault_tag>/critic-findings-dialectic.json` (counter-evidence the draft missed or straw-manned)
   - `hyperresearch-depth-critic` → `research/runs/<vault_tag>/critic-findings-depth.json` (shallow spots where interim notes could fill substance)
   - `hyperresearch-width-critic` → `research/runs/<vault_tag>/critic-findings-width.json` (corpus clusters the draft ignores despite evidence)
   - `hyperresearch-instruction-critic` → `research/runs/<vault_tag>/critic-findings-instruction.json` (atomic items from the decomposition that the draft missed, under-covered, reordered, or reformatted)

2. **Pass each critic** (standard 3-piece contract):
   ```
   custom_agent: hyperresearch-<critic-name>-critic   # spawn the custom agent defined in .codex/agents/hyperresearch-<critic-name>-critic.toml
   prompt: |
     RESEARCH QUERY (verbatim, gospel):
     > {{paste research/runs/<vault_tag>/query.md body}}

     QUERY FILE: research/runs/<vault_tag>/query.md

     PIPELINE POSITION: You are step 12 (<critic-name> critic) of the
     hyperresearch V8 pipeline. Step 11 (synthesizer) produced the final report at
     research/notes/final_report_<vault_tag>.md. After you return, step 13 may run a
     gap-fetch wave, then step 14 (patcher) applies findings as patch hunks.

     YOUR INPUTS:
     - draft_path: research/notes/final_report_<vault_tag>.md
     - output_path: research/runs/<vault_tag>/critic-findings-<critic-name>.json
     - vault_tag: <vault_tag>
     - decomposition_path: research/runs/<vault_tag>/prompt-decomposition.json   (instruction-critic only)

     RUN DIRECTIVES: append the FULL contents of research/runs/<vault_tag>/shims/critics.md here, verbatim.
   ```

3. **Wait for all critics.** If one fails, re-spawn it once. If it fails again, you can proceed with the partial set: write its output file yourself as `{"findings": [], "critic_failed": "<reason>"}` (the ship gate requires all four findings files to exist) and log the absence to the run log — the patch pass is less robust with missing critic coverage. **Do NOT skip the instruction-critic specifically** — it's the only critic measuring prompt adherence, which is the dimension with the widest variance.

4. **Do not read the findings yourself and apply them.** The patcher (step 14) reads the findings. Your job is to hand them to the patcher — AFTER step 13 (gap-fetch) runs.
<% else %>Run the four critics through GPT-6.2 Astra at `xhigh` via Codex CLI.

```bash
"{hpr_path}" run critics <vault_tag> --json
```

The command supplies the canonical query, draft, step 1 decomposition, vault note
inventory, and `shims/critics.md` directives. It launches one read-only Codex
review for each role: dialectic, depth, width, and instruction. Wait for the
command to finish and read its JSON result. Each completed review records a
Codex thread ID and a JSONL log.

On failure, report the error and log paths, mark step 12 failed, and retry
after addressing the cause. Advance to step 13 after a successful result
with four findings files.
<% endif %>

---

## Exit criterion

- All 4 critic findings JSONs exist (`research/runs/<vault_tag>/critic-findings-<name>.json`)<% if platform == "codex" %>, a failed critic's as the empty `critic_failed` stub from step 3<% endif %>
- Each is valid JSON with a `findings` array

---

## Next step

Return to the entry skill (`hyperresearch`). Invoke step 13:

```
<% if platform == "codex" %>cat .hyperresearch/codex/steps/hyperresearch-13-gap-fetch.md<% else %>Skill(skill: "hyperresearch-13-gap-fetch")<% endif %>
```

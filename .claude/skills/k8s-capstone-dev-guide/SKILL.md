---
name: k8s-capstone-dev-guide
description: Operational checklist and runbook for working inside the LLM-enabled Zero-Touch Kubernetes Service Deployment Platform capstone repo (natural language -> DeploySpec -> agents/orchestrator -> guardian -> gitops -> healer). Use this whenever you are about to change code in this repo, diagnose why the Model Server / RAG / K8s pipeline isn't behaving, decide whether a change counts as "big" and needs a plan first, or wrap up a change and aren't sure whether docs/tests need updating. Also use it when the user reports something that "looks broken" in this platform (deploy stuck, Chat giving wrong answers, Healer not firing) before assuming it's a code bug — check the live environment first.
---

# K8s Capstone Dev Guide

This repo already force-loads `CLAUDE.md` and `AGENT_RULES.md` into every session, so you know the *rules*. This skill is the *checklist* — the concrete steps that turn those rules into something you actually run through, instead of knowing-but-forgetting mid-task. Read the relevant section below, run the steps, don't just summarize them back.

## 1. Before touching anything: is the environment even alive?

A huge fraction of "this feature is broken" reports in this project turn out to be an environment that silently isn't running, not a logic bug. Check this FIRST, before reading code, whenever the report is behavioral ("it gave a weird answer", "deploy didn't happen", "pods aren't healing") rather than an explicit stack trace.

```bash
curl http://127.0.0.1:8765/health      # Model Server: look for deploy_loaded / monitor_loaded = true
kubectl get nodes                       # K8s reachable at all?
kubectl get pods -n monitoring          # if the report touches Metrics/Healer real_usage
```

If `deploy_loaded`/`monitor_loaded` is false, or the curl fails outright, say so to the user explicitly and tell them how to start it (`python core/model_server.py`) — don't quietly fall back to reading the inference code and guessing. This mirrors the project's own "no silent failure" rule (AGENT_RULES.md) — it applies to you debugging, not just to the app's UI.

## 2. Deciding if a change is "big" (needs a Plan first)

Per AGENT_RULES.md, a change needs EnterPlanMode + explicit user approval before you touch files if it's any of:

- touches more than one stage of the pipeline (translation layer / RAG / model server / agents / guardian / gitops / healer)
- adds a new subsystem, agent, API route group, or frontend page
- touches `training/` scripts or the fine-tuning data format
- adds/changes a dependency, or touches CI/deploy config
- deletes data, rewrites existing logic broadly, or is otherwise hard to undo

If none of those apply — single-file bug fix, typo, small tweak to one existing function, a setting value, docs — go ahead directly. When genuinely unsure which bucket a request falls into, say which bucket you think it's in and why, rather than silently picking the convenient one.

## 3. If the change touches the deploy pipeline: three-layer check

Deploy-path changes (anything between "user describes a deployment" and "it's running in the cluster") must keep all three layers intact — don't let a fix in one layer become a bypass of another:

1. **`agents/orchestrator`** — policy decision (security/cost/perf -> approve/warn/block)
2. **`guardian`** — format/safety validation (yaml_validator, dry_run)
3. **`gitops`** — version control (manifest_writer, argocd_sync)

Ask yourself explicitly: "does my change still let guardian run its dry-run before gitops commits?" and "can orchestrator still block this path?" If a fix makes it tempting to skip one layer for convenience (e.g. writing straight to gitops for a 'simple' case), that's a sign to stop and reconsider, not a shortcut to take.

## 4. New-user-friendliness gate (applies to ANY UI/UX-facing change, not just new features)

Before considering a frontend or error-handling change done, check it against AGENT_RULES.md's core principle:

- No silent failure — any state that could strand a beginner (Docker/K8s down, model server down, deploy blocked, action failed) must show up on screen, not just in a log.
- Says "what to do", not just "what's wrong" — "請先開啟 Docker Desktop，等它完全啟動後重新整理" beats "Not connected".
- Live status, not cached — don't reuse a startup-time check result for a "connected" indicator shown later.
- Prominent, not a dot in a corner, for anything the user needs to notice.

If you're adding a new failure mode and skip one of these, name which one and why — don't let it pass silently.

## 5. Before saying a change is done

Run through, in this order:

1. **Tests.** If you touched `agents/`, `guardian/yaml_validator.py`, `healer/diagnose.py`, or `healer/remediate.py`: run `pytest` (use `pytest -m "not integration"` if you don't have the model server / cluster up). Don't report completion without having actually run it in this session — "it should pass" is not "it passed".
2. **`docs/security_review.md`.** If what you fixed/found is the kind of thing an evaluator or reviewer would ask about (a security gap, a data-consistency issue, LLM hallucination behavior, a reliability or performance tradeoff) — add an entry: 風險 → 為什麼重要 → 解決方式 → 驗證(with real numbers if you have them). Small single-file bug fixes don't need this.
3. **`CHANGELOG.md`.** If the change has real weight (new feature, fixed bug, architecture adjustment) add a dated entry now, in your own words, not a generic "updated files" line. Skip for typos/tiny tweaks.
4. **Don't trust `docs/roadmap.md` or other docs as ground truth for "is X already built".** This project has been burned by that before (see CLAUDE.md's own warning about this). Grep the actual call path (e.g. `grep -rn "from agents.orchestrator"`) before claiming something is or isn't wired up.

## 6. Common traps specific to this codebase

- `healer/__init__.py` does `from healer.remediate import remediate`, which shadows the `remediate` submodule name with the function. `patch("healer.remediate.xxx")` or `import healer.remediate as X` will silently resolve to the function, not the module. Pull the real module via `sys.modules["healer.remediate"]` in tests instead.
- pytest test files that need `torch`/`transformers`/`flask` at import time MUST use `pytest.importorskip("torch")` (or similar), never a bare top-level `import` — a bare import failure happens during collection, before `-m` filtering applies, and fails the entire `pytest` run, not just that one test. CI deliberately doesn't install those packages.
- Windows + `subprocess.run(text=True)` defaults to cp950 decoding, which breaks the instant a Chinese commit message or log line shows up. Pass `encoding="utf-8", errors="replace"` explicitly on anything that shells out to `git`.
- `kubernetes` python client retries 3x by default with no explicit `retries=0`/timeout tuple set — a single bad connection attempt can silently balloon to tens of seconds. If you're adding a new K8s API call that should fail fast, set an explicit `Configuration` with `retries = 0` and a `(connect, read)` timeout tuple, not just `_request_timeout`.
- Every account gets its own namespace (`user-<username>`), default `NS="default"` is a fallback only. Any new function touching K8s resources needs to accept/pass `namespace` — don't assume `default`. The two intentional cross-namespace exceptions are node capacity checks and port-conflict checks (see CLAUDE.md 2026-09-15 entry).

## When this skill does NOT apply

Pure documentation-only requests the user already fully specified ("fix this typo", "translate this report"), or work entirely outside this repo, don't need this checklist — just do the thing.

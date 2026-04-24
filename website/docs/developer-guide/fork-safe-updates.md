---
sidebar_position: 8
title: "Fork-Safe Upstream Updates"
description: "How to update a customized Hermes fork without losing local product behavior or messaging patches"
---

# Fork-Safe Upstream Updates

This protocol is for teams running a **customized Hermes fork** and regularly pulling changes from the public upstream.

It exists to protect local product behavior while still benefiting from upstream improvements.

Typical examples of fork-only behavior that must survive updates:

- Telegram routing or mention semantics
- iPhone / companion-app integration surfaces
- OpenClaw-specific runtime bridges
- deployment and operational guardrails
- product-specific UX or policy behavior

## Core rule

**Never update a customized fork by pulling directly into the live branch and hoping for the best.**

Use an integration branch, compare the delta explicitly, revalidate fork-only behavior, then merge only after checks are green.

---

## Golden model

Think in three layers:

1. **Upstream** = community source of truth for generic Hermes improvements
2. **Fork** = sovereign product layer for your custom behavior
3. **Runtime / production** = only what has already passed integration validation

The goal of an update is **not** to make the fork look identical to upstream.
The goal is to **absorb safe upstream value without deleting intentional local behavior**.

---

## When to use this protocol

Use this protocol whenever:

- the repo is a GitHub fork of `NousResearch/hermes-agent`
- the fork contains custom patches not yet upstreamed
- a release tag or `upstream/main` has moved ahead
- you are preparing a production update for CLI, gateway, Telegram, or app integrations

Do **not** skip this process just because Git says the merge is automatic.
A clean merge can still silently change runtime behavior.

---

## Required preconditions

Before starting an update, verify all of the following:

1. local working tree is clean
2. the current production branch is pushed
3. `upstream` remote exists and points to `https://github.com/NousResearch/hermes-agent.git`
4. fork-only patches are listed in writing
5. rollback target is known in advance

Recommended commands:

```bash
git status --short
git remote -v
git fetch origin --tags
git fetch upstream --tags
```

---

## Maintain a fork-only patch ledger

Every customized fork should keep a short ledger of the behavior that must survive updates.

Minimum fields:

- commit hash
- short title
- files touched
- runtime behavior protected
- how to test it

### Current example: Telegram-specific patches

If your fork contains local Telegram semantics, record them explicitly before every update. For example:

- `fix(telegram): ignore messages for other bots in groups`
- `fix(telegram): let first mentioned bot own group thread`

These are not cosmetic differences. They are product behavior and must be treated like compatibility guarantees.

---

## Choose the update target

There are two valid targets:

### Option A — latest stable release tag

Prefer this when:

- the upstream just shipped a release
- there are fresh bug reports on `upstream/main`
- you want a safer update window

Example:

```bash
git fetch upstream --tags
git tag -l 'v*' --sort=-version:refname | head
```

### Option B — `upstream/main`

Use this only when:

- a specific upstream fix is urgently needed
- that fix has not yet landed in a release
- you are ready to absorb more instability

**Default preference: release tag over raw `upstream/main`.**

---

## Create an integration branch

Never perform the first merge on your live branch.

```bash
git checkout main
git pull origin main
git checkout -b chore/upstream-integration-YYYYMMDD
```

If updating to a release tag:

```bash
git merge --no-ff v2026.4.23
```

If updating to upstream main:

```bash
git merge --no-ff upstream/main
```

---

## Inspect the overlap before trusting the merge

After the merge attempt, inspect where upstream changed the same surfaces as the fork.

Recommended checks:

```bash
# commits coming from upstream
git log --oneline main..HEAD

# files changed versus the pre-update branch
git diff --name-only main...HEAD

# focus on messaging/platform surfaces
git diff --name-only main...HEAD -- gateway/platforms/ hermes_cli/ tools/ website/docs/
```

High-risk directories for customized forks typically include:

- `gateway/platforms/`
- `gateway/run.py`
- `hermes_cli/`
- `tools/`
- `agent/`
- app-specific bridge or API layers

---

## Conflict policy

When a conflict appears, do **not** resolve it mechanically.

Use this decision rule:

### Upstream wins when

- the change is a generic bug fix
- the change improves safety, correctness, or compatibility
- the local fork was only carrying a temporary workaround now superseded upstream

### Fork wins when

- the behavior is product-specific
- the behavior is required for Telegram thread ownership, bot routing, iPhone app compatibility, or OpenClaw interoperability
- removing it would change user-visible semantics you intentionally rely on

### Manual synthesis when

- both upstream and fork are correct but solving different concerns
- upstream changed structure while the fork changed behavior
- the right answer is to port the local behavior onto the new upstream architecture

---

## Mandatory validation gates

An update is not ready just because it merges.

Run validation in this order.

### 1. Git hygiene

```bash
git status --short
git diff --stat main...HEAD
```

### 2. Python / dependency sanity

```bash
source venv/bin/activate
uv pip install -e ".[all]"
```

### 3. Fast test path

```bash
pytest -q
```

If the full suite is too large, at minimum run the tests covering the touched surfaces.

### 4. Targeted fork-behavior validation

For every fork-only patch, run a focused scenario test.

For Telegram patches, validate at least:

- message in a group mentioning another bot only → Hermes ignores it
- message mentioning Hermes first in a multi-bot thread → Hermes owns the thread
- direct mention to Hermes still works
- unrelated Telegram behavior did not regress

If automated tests do not exist yet, perform a manual runtime check and record the result in the PR description.

### 5. Runtime smoke test

Validate the actual runtime surface you care about:

- CLI starts
- gateway starts
- Telegram still responds correctly
- any iPhone companion app contract still works
- any OpenClaw bridge still works

---

## Release readiness checklist

Only promote the integration branch when all answers are yes:

- [ ] update target chosen intentionally (tag or `upstream/main`)
- [ ] local fork-only patches inventoried
- [ ] merge completed without unresolved conflicts
- [ ] overlapping high-risk files reviewed manually
- [ ] targeted behavior checks passed
- [ ] runtime smoke test passed
- [ ] rollback target documented

---

## Rollback plan

Before shipping, record both:

- the current production commit
- the candidate update commit

If production regresses after deployment:

```bash
git checkout <last-known-good-commit>
source venv/bin/activate
uv pip install -e ".[all]"
hermes gateway restart
```

Never improvise rollback under pressure. Pre-select the commit before deployment.

---

## Recommended PR template additions for fork updates

When opening a PR for an upstream sync, include:

### Update target

- release tag or upstream commit
- why that target was chosen

### Fork-only behaviors protected

- list of local patches expected to survive
- files or modules reviewed manually

### Validation

- tests run
- manual Telegram checks run
- app / bridge checks run

### Risk notes

- any unresolved upstream instability
- any behavior still requiring observation after deploy

---

## Practical default for this fork

For this fork, the safe default is:

1. compare `origin/main` against the latest upstream release tag
2. avoid raw `upstream/main` when fresh P1/P2 bugs are still landing
3. protect Telegram-specific routing semantics as explicit product behavior
4. validate any future iPhone companion-app integration before promoting the update
5. validate OpenClaw interoperability whenever gateway or messaging code changed

---

## Short version

If you remember only one thing, remember this:

> **Upstream updates are merges of value, not permission to delete local intent.**

Use a branch. Inventory local behavior. Merge carefully. Revalidate product semantics. Then ship.

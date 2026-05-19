# Hermes Learning System

Hermes Learning System is a generic, offline-first learning layer for turning session signals into reusable instincts.

This is intentionally **not** a Raphaël-private doctrine store. The fork contains only the generic mechanism:

- observations;
- scoped instincts;
- confidence scores;
- project/global separation;
- cross-project promotion;
- status exports that do not leak raw observation text.

Private operator doctrine, business strategy, secrets, project-specific production rules, and second-brain links belong in a private overlay or Obsidian, not in the public fork.

## Why it exists

Hermes already has memory, skills, session search, and Obsidian workflows. This package adds a small deterministic primitive that can later be connected to hooks, gateway events, cron review jobs, or a CAG/context router.

The first slice proves the core loop:

```text
observation/correction
  -> deterministic instinct
  -> scope: global or project
  -> confidence
  -> persistence
  -> promotion when seen across projects
```

## Storage

By default, the CLI stores data under:

```text
$HERMES_HOME/learning
```

Override with:

```bash
export HERMES_LEARNING_HOME=/path/to/learning-store
```

Or per command:

```bash
python -m plugins.hermes_learning_system.cli --root /tmp/hls status
```

## CLI examples

Ingest a correction:

```bash
PYTHONPATH=. python -m plugins.hermes_learning_system.cli \
  ingest \
  --kind user_correction \
  --source telegram \
  --project-id dbm \
  --tag learning \
  'Do not learn a capability as project-only; make it transferable.'
```

Show status without raw observation text:

```bash
PYTHONPATH=. python -m plugins.hermes_learning_system.cli status
```

Run built-in offline eval probes:

```bash
PYTHONPATH=. python -m plugins.hermes_learning_system.cli eval
```

Promote repeated project instincts:

```bash
PYTHONPATH=. python -m plugins.hermes_learning_system.cli promote \
  --min-projects 2 \
  --min-average-confidence 0.8
```

## Current slice boundaries

Included now:

- deterministic engine;
- dataclasses for `Observation` and `Instinct`;
- global/project scoping;
- confidence updates;
- cross-project promotion;
- CLI status/ingest/promote/eval;
- deterministic eval fixtures for scope, confidence, and privacy leakage;
- tests and smoke command.

Not included yet:

- automatic gateway/session hooks;
- LLM-based extraction from full transcripts;
- approval UI for promotions;
- automatic skill writing;
- Obsidian write-back;
- CAG/context-router integration.

Those should be later slices after the primitive is stable.

## Safety model

- Public fork stores generic code only.
- `status` exports counts and IDs, not raw observation text.
- Observation metadata stores a short SHA-256 hint, not content.
- Private learning stores can keep richer evidence outside this repo if the operator chooses.

## Tests

```bash
PYTHONPATH=. python -m pytest \
  tests/plugins/test_hermes_learning_system.py \
  tests/plugins/test_hermes_learning_system_cli.py \
  tests/plugins/test_hermes_learning_system_eval.py -q
```

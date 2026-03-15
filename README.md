# keshro-cli

Thin CLI for Keshro's hosted API.

## Alpha access

To use the private alpha CLI, a user needs:

1. A Keshro account
2. CLI access enabled on the Keshro backend
3. Read access to this private GitHub repo
4. `uv` installed locally

## Install

Private repo install with `uv` over SSH:

```bash
uv tool install git+ssh://git@github.com/jlewitt1/keshro-cli.git
```

Update an existing install:

```bash
uv tool install --force git+https://github.com/jlewitt1/keshro-cli.git
```

Local development install:

```bash
uv tool install .
```

## Configure

By default the CLI talks to `http://localhost:8000`.

Set a hosted API base URL with:

```bash
export KESHRO_API_URL="https://app.keshro.com"
```

## Auth

```bash
keshro
kr
keshro login ksh_pat_...
keshro logout
```

Auth state is stored in `~/.keshro/auth.json`.

Create API tokens from the Keshro Account -> API page. If CLI access is not enabled for the account, authenticated CLI commands will return `403`.

## Commands

```bash
keshro plan create --title "AWS Batch to Airflow" --source-type "AWS Batch" --target-type "Airflow"
keshro task next -p <plan-id>
keshro task start <task-id> -p <plan-id>
keshro task note <task-id> -p <plan-id> --note "Airflow is orchestrating existing Batch jobs during pilot"
keshro task artifact <task-id> -p <plan-id> --link "https://github.com/acme/migrations/pull/19"
keshro task block <task-id> -p <plan-id> --reason "Waiting on Terraform IAM role changes"
keshro task unblock <task-id> -p <plan-id> --notes "IAM fix applied; resuming pilot"
keshro task done <task-id> -p <plan-id> --notes "Pilot merged"
keshro migration history <migration-id>
keshro plan replan-notes "Need hybrid Airflow + Batch rollout" -p <plan-id>
keshro plan view <plan-id>
keshro plan list
keshro plan update <plan-id> --status ready
```

## Execution loop behavior

The CLI is designed to let Claude Code or another coding agent keep Keshro current while the work is happening, not after the fact.

Default write-now events:

- `keshro task start`
- `keshro task note`
- `keshro task artifact`
- `keshro task block`
- `keshro task unblock`

Ask first before writing:

- `keshro task done`
- `keshro task delete`
- `keshro plan replan-notes` when the change materially alters migration scope or sequencing

Concrete examples:

- Claude starts editing the next task's files:
  - run `keshro task start <task-id> -p <plan-id>`
- Claude discovers Airflow should orchestrate Batch during the pilot:
  - run `keshro task note <task-id> -p <plan-id> --note "Airflow will orchestrate Batch during pilot"`
- Claude opens a PR:
  - run `keshro task artifact <task-id> -p <plan-id> --link "<pr-url>"`
- Claude hits a Terraform/IAM blocker:
  - run `keshro task block <task-id> -p <plan-id> --reason "Waiting on Terraform IAM role changes"`
- The blocker is resolved:
  - run `keshro task unblock <task-id> -p <plan-id> --notes "IAM fix applied; resuming pilot"`
- Claude believes the task is done:
  - ask the user first, then run `keshro task done <task-id> -p <plan-id> --notes "<what landed>"`

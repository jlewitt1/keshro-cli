# keshro-cli

Thin CLI for Keshro's hosted API.

## Install

Private repo install with `uv`:

```bash
uv tool install git+ssh://git@github.com/jlewitt1/keshro-cli.git
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
keshro auth login --email you@example.com --password '...'
keshro auth whoami
```

Auth state is stored in `~/.keshro/auth.json`.

## Commands

```bash
keshro plan create --title "AWS Batch to Airflow" --source-type "AWS Batch" --target-type "Airflow"
keshro plan list
keshro plan view <plan-id>
keshro plan update <plan-id> --status ready
keshro outcome save <plan-id> --status completed --summary "Cutover done"
keshro outcome view <plan-id>
```

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

Create API tokens from the Keshro Account -> API page. If CLI access is not enabled for the account, authenticated plan and outcome commands will return `403`.

## Commands

```bash
keshro plan create --title "AWS Batch to Airflow" --source-type "AWS Batch" --target-type "Airflow"
keshro plan list
keshro plan view <plan-id>
keshro plan update <plan-id> --status ready
keshro outcome save <plan-id> --status completed --summary "Cutover done"
keshro outcome view <plan-id>
```

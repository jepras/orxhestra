# How to run orx

One-liner (from repo root):

```bash
source .venv/bin/activate && set -a && source .env && set +a && orx
```

This activates the venv, loads `.env` (`OPENAI_API_KEY` + `OPENAI_BASE_URL` for Azure Foundry), and starts `orx` using `./orx.yaml` (which pins the deployment name `gpt-5.5`).

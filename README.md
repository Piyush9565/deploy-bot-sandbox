# deploy-bot-sandbox

Test bed for a Slack `/deploy` bot that syncs ArgoCD applications, as a non-agent alternative
to the Release Agent idea. Only allowlisted Slack users in allowlisted channels can deploy, and
the bot only syncs what is already merged here.

| Path | What it is |
|---|---|
| `apps/podinfo/values.yaml` | Stand-in for ped-sot. Bump `image.tag`, merge to `main`, then run `/deploy podinfo` in Slack. |
| `bot/` | The bot (Python, Slack Socket Mode, ArgoCD API). |
| `k8s/bot.yaml` | Bot Deployment and settings for the test cluster. |
| `k8s/podinfo-app.yaml` | Test ArgoCD app shaped like a Peddle prod app: chart from OCI, values from Git, manual sync. |
| `scripts/deploy-bot-secrets.sh` | Run on the test box to store the Slack and ArgoCD tokens. No secrets live in this repo. |

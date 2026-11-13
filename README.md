# deploy-bot-sandbox

Test bed for a Slack `/deploy` bot that syncs ArgoCD applications, as a non-agent alternative
to the Release Agent idea. Only allowlisted Slack users in allowlisted channels can deploy, and
the bot only syncs what is already merged here. Laid out like Peddle prod: this repo plays the
part of ped-sot (values) and devops-infrastructure (ArgoCD apps).

| Path | What it is |
|---|---|
| `apps/podinfo/values.yaml` | Test app's values (stand-in for a service in ped-sot). Change `image.tag`, merge to `main`, then run `/deploy podinfo` in Slack. |
| `apps/slack-deploy-bot/` | Plain Kubernetes manifests for the bot (no Helm): ServiceAccount, Deployment with the ECR image, and the External Secrets generator that keeps the ECR pull secret fresh. The bot reads its tokens and allowlist from SSM Parameter Store itself, once at startup. |
| `argocd/` | ArgoCD apps and project: External Secrets, the `devops` project, the bot, and podinfo. |
| `bot/` | Bot code (Python, Slack Socket Mode, ArgoCD API), Dockerfile, and CodeBuild buildspec. |
| `scripts/deploy-bot-ssm-secrets.sh` | Run on the test box to store the Slack and ArgoCD tokens in SSM. No secrets live in this repo. |

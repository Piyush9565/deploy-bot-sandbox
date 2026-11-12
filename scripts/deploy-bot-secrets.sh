#!/bin/bash
# Run on the test instance (sudo, inside an SSM session). Stores the Slack tokens and a fresh
# ArgoCD API token for the slack-deployer account in the slack-deploy-bot Secret.
# Inputs are hidden and nothing secret is printed or passed on a command line.
set -euo pipefail
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml HOME=/root

read -rsp "Slack bot token (xoxb-...): " SLACK_BOT_TOKEN; echo
read -rsp "Slack app-level token (xapp-...): " SLACK_APP_TOKEN; echo
if [[ $SLACK_BOT_TOKEN != xoxb-* || $SLACK_APP_TOKEN != xapp-* ]]; then
  echo "Those don't look like xoxb- and xapp- tokens. Nothing was saved." >&2
  exit 1
fi

echo "Log in to ArgoCD as admin, so the script can create the bot's API token:"
argocd login 127.0.0.1:30443 --insecure --username admin
# Revoke earlier bot tokens (including ones from aborted runs) so only the new one works.
for id in $(argocd account get --account slack-deployer -o json \
    | python3 -c 'import json, sys; print(" ".join(t["id"] for t in json.load(sys.stdin).get("tokens") or []))'); do
  argocd account delete-token --account slack-deployer "$id" >/dev/null
done
ARGOCD_TOKEN=$(argocd account generate-token --account slack-deployer --id "slack-deploy-bot-$(date +%Y%m%d%H%M%S)")
# `argocd logout` can hang on this setup, so cap it and drop the local admin session either way.
timeout 15 argocd logout 127.0.0.1:30443 >/dev/null 2>&1 || true
rm -f "$HOME/.config/argocd/config"

env_file=$(mktemp -p /dev/shm)
trap 'rm -f "$env_file"' EXIT
chmod 600 "$env_file"
printf 'SLACK_BOT_TOKEN=%s\nSLACK_APP_TOKEN=%s\nARGOCD_TOKEN=%s\n' \
  "$SLACK_BOT_TOKEN" "$SLACK_APP_TOKEN" "$ARGOCD_TOKEN" > "$env_file"
kubectl -n slack-deploy-bot create secret generic slack-deploy-bot \
  --from-env-file="$env_file" --dry-run=client -o yaml | kubectl apply -f -
echo "Saved. Tell Claude the secret is in place."

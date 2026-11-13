#!/bin/bash
# Run on the test instance (sudo, inside an SSM session). Stores the bot's three tokens in SSM
# Parameter Store as SecureString under /slack-deploy-bot/. Copies them from the cluster Secret
# left by the first test if it's still there; otherwise asks for the Slack tokens and creates a
# new ArgoCD token for slack-deployer. Nothing secret is printed or passed on a command line.
set -euo pipefail
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml HOME=/root AWS_REGION=ap-southeast-2
OLD_NS=slack-deploy-bot
OLD_SECRET=slack-deploy-bot

old_value() { kubectl -n "$OLD_NS" get secret "$OLD_SECRET" -o "jsonpath={.data.$1}" | base64 -d; }

if kubectl -n "$OLD_NS" get secret "$OLD_SECRET" >/dev/null 2>&1; then
  echo "Copying the tokens from the existing cluster secret $OLD_NS/$OLD_SECRET."
  SLACK_BOT_TOKEN=$(old_value SLACK_BOT_TOKEN)
  SLACK_APP_TOKEN=$(old_value SLACK_APP_TOKEN)
  ARGOCD_TOKEN=$(old_value ARGOCD_TOKEN)
else
  read -rsp "Slack bot token (xoxb-...): " SLACK_BOT_TOKEN; echo
  read -rsp "Slack app-level token (xapp-...): " SLACK_APP_TOKEN; echo
  echo "Log in to ArgoCD as admin, so the script can create the bot's API token:"
  argocd login 127.0.0.1:30443 --insecure --username admin
  ARGOCD_TOKEN=$(argocd account generate-token --account slack-deployer --id "slack-deploy-bot-$(date +%Y%m%d%H%M%S)")
  timeout 15 argocd logout 127.0.0.1:30443 >/dev/null 2>&1 || true
  rm -f "$HOME/.config/argocd/config"
fi
if [[ $SLACK_BOT_TOKEN != xoxb-* || $SLACK_APP_TOKEN != xapp-* || -z $ARGOCD_TOKEN ]]; then
  echo "The tokens don't look right. Nothing was saved." >&2
  exit 1
fi
export SLACK_BOT_TOKEN SLACK_APP_TOKEN ARGOCD_TOKEN

request=$(mktemp -p /dev/shm)
trap 'rm -f "$request"' EXIT
chmod 600 "$request"
for name in SLACK_BOT_TOKEN SLACK_APP_TOKEN ARGOCD_TOKEN; do
  python3 -c 'import json, os, sys
json.dump({"Name": "/slack-deploy-bot/" + sys.argv[1], "Value": os.environ[sys.argv[1]],
           "Type": "SecureString", "Overwrite": True}, open(sys.argv[2], "w"))' "$name" "$request"
  aws ssm put-parameter --cli-input-json "file://$request" >/dev/null
  echo "Saved /slack-deploy-bot/$name (SecureString)"
done
echo "Done. Tell Claude the tokens are in SSM."

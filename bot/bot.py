"""Slack /deploy bot for ArgoCD (test prototype).

Only allowlisted Slack users, in allowlisted channels, can deploy. The bot only
syncs what is already merged in Git: it never picks images or edits values, and
every sync is pinned to the exact revisions shown in the confirmation message.
"""

import json
import os
import re
import threading
import time

import requests
import urllib3
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

ARGOCD_URL = os.environ.get("ARGOCD_URL", "https://argocd-server.argocd.svc").rstrip("/")
ARGOCD_VERIFY_TLS = os.environ.get("ARGOCD_VERIFY_TLS", "false").lower() == "true"
ALLOWED_USERS = {u.strip() for u in os.environ["ALLOWED_USER_IDS"].split(",") if u.strip()}
ALLOWED_CHANNELS = {c.strip() for c in os.environ["ALLOWED_CHANNEL_IDS"].split(",") if c.strip()}
SYNC_TIMEOUT_SECONDS = int(os.environ.get("SYNC_TIMEOUT_SECONDS", "600"))
CONFIRM_TTL_SECONDS = int(os.environ.get("CONFIRM_TTL_SECONDS", "300"))
POLL_SECONDS = 5
APP_NAME = re.compile(r"[a-z0-9]([-a-z0-9]{0,251}[a-z0-9])?")

argocd = requests.Session()
argocd.headers["Authorization"] = f"Bearer {os.environ['ARGOCD_TOKEN']}"
argocd.verify = ARGOCD_VERIFY_TLS
if not ARGOCD_VERIFY_TLS:
    urllib3.disable_warnings()  # the test cluster uses ArgoCD's self-signed certificate

app = App(token=os.environ["SLACK_BOT_TOKEN"])

# Apps with a deploy in progress, so a double click can't start two syncs.
in_flight = set()
in_flight_lock = threading.Lock()


def audit(event, **fields):
    """One JSON line per decision, so `kubectl logs` doubles as the audit trail."""
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    print(json.dumps({"time": now, "event": event, **fields}), flush=True)


def allowed(user_id, channel_id):
    return user_id in ALLOWED_USERS and channel_id in ALLOWED_CHANNELS


def short(revision):
    return revision[:7] if re.fullmatch(r"[0-9a-f]{40}", revision) else revision


def code_list(items):
    return ", ".join(f"`{i}`" for i in items) or "_none_"


# --- ArgoCD API ---------------------------------------------------------------


def get_app(name, refresh=False):
    """Return the Application, or None if it doesn't exist or the bot can't see it."""
    params = {"refresh": "normal"} if refresh else None
    r = argocd.get(f"{ARGOCD_URL}/api/v1/applications/{name}", params=params, timeout=60)
    if r.status_code in (403, 404):  # ArgoCD answers 403 for unknown apps too
        return None
    r.raise_for_status()
    return r.json()


def is_multi_source(application):
    return bool(application["spec"].get("sources"))


def target_revisions(application):
    """Resolved revisions (Git SHA, chart version) ArgoCD would deploy right now."""
    sync = application["status"].get("sync", {})
    return sync.get("revisions") or ([sync["revision"]] if sync.get("revision") else [])


def pinned_query(revisions, multi_source):
    if multi_source:
        return {"revisions": revisions, "sourcePositions": list(range(1, len(revisions) + 1))}
    return {"revision": revisions[0]}


def rendered_images(name, revisions, multi_source):
    """Container images in the manifests ArgoCD renders for the pinned revisions."""
    r = argocd.get(
        f"{ARGOCD_URL}/api/v1/applications/{name}/manifests",
        params=pinned_query(revisions, multi_source),
        timeout=120,
    )
    r.raise_for_status()
    images = set()
    for doc in r.json().get("manifests") or []:
        spec = json.loads(doc).get("spec") or {}
        pod = (spec.get("template") or {}).get("spec") or (
            ((spec.get("jobTemplate") or {}).get("spec") or {}).get("template") or {}
        ).get("spec") or {}
        for container in pod.get("initContainers", []) + pod.get("containers", []):
            images.add(container["image"])
    return sorted(images)


def build_plan(application):
    name = application["metadata"]["name"]
    status = application["status"]
    multi_source = is_multi_source(application)
    revisions = target_revisions(application)
    return {
        "name": name,
        "multi_source": multi_source,
        "revisions": revisions,
        "live_images": sorted(status.get("summary", {}).get("images", [])),
        "target_images": rendered_images(name, revisions, multi_source),
        "sync_status": status.get("sync", {}).get("status", "Unknown"),
        "health": status.get("health", {}).get("status", "Unknown"),
    }


def start_sync(name, revisions, multi_source):
    body = {"prune": False, **pinned_query(revisions, multi_source)}
    r = argocd.post(f"{ARGOCD_URL}/api/v1/applications/{name}/sync", json=body, timeout=60)
    if r.status_code >= 400:
        try:
            message = r.json().get("message", r.text)
        except ValueError:
            message = r.text
        raise RuntimeError(f"ArgoCD refused the sync ({r.status_code}): {message[:300]}")


def wait_for_rollout(name, started_at, expected_images):
    """Healthy means: our sync succeeded, health is Healthy and the live images match."""
    deadline = time.time() + SYNC_TIMEOUT_SECONDS
    phase = health = None
    while time.time() < deadline:
        time.sleep(POLL_SECONDS)
        status = get_app(name)["status"]
        operation = status.get("operationState") or {}
        if operation.get("startedAt", "") < started_at:
            continue  # still looking at the previous sync
        phase = operation.get("phase")
        health = status.get("health", {}).get("status")
        if phase in ("Failed", "Error"):
            return False, f"sync {phase.lower()}: {operation.get('message', '')[:300]}"
        live = sorted(status.get("summary", {}).get("images", []))
        if phase == "Succeeded" and health == "Healthy" and live == expected_images:
            return True, "synced, healthy, and running the expected images"
    minutes = SYNC_TIMEOUT_SECONDS // 60
    return False, f"timed out after {minutes} min (sync: {phase}, health: {health})"


# --- Slack messages -------------------------------------------------------------


def plan_text(plan, user):
    return "\n".join([
        f"*Deploy `{plan['name']}`?* Requested by <@{user}>",
        f"Now running: {code_list(plan['live_images'])}",
        f"Will deploy: {code_list(plan['target_images'])}",
        f"Pinned revisions: {code_list(short(r) for r in plan['revisions'])}",
        f"ArgoCD: {plan['sync_status']}, {plan['health']}",
    ])


def section(text):
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def confirm_blocks(plan, user, value):
    return [
        section(plan_text(plan, user)),
        {
            "type": "actions",
            "elements": [
                {"type": "button", "action_id": "deploy_confirm", "style": "primary",
                 "text": {"type": "plain_text", "text": "Deploy"}, "value": value},
                {"type": "button", "action_id": "deploy_cancel",
                 "text": {"type": "plain_text", "text": "Cancel"}, "value": value},
            ],
        },
    ]


def close_message(client, body, note):
    """Replace the buttons with a note, keeping the original summary."""
    summary = body["message"]["blocks"][0]["text"]["text"]
    client.chat_update(
        channel=body["channel"]["id"], ts=body["message"]["ts"],
        text=note, blocks=[section(summary), section(note)],
    )


# --- Slack handlers --------------------------------------------------------------


@app.command("/deploy")
def deploy_command(ack, command, respond, client):
    ack()
    user, channel = command["user_id"], command["channel_id"]
    name = command.get("text", "").strip()
    if not allowed(user, channel):
        audit("deploy_denied", user=user, channel=channel, app=name)
        respond("You're not allowed to deploy from here.")
        return
    if not APP_NAME.fullmatch(name):
        respond("Usage: `/deploy <application-name>`")
        return
    try:
        application = get_app(name, refresh=True)
        if application is None:
            respond(f"I can't find an application called `{name}` that I'm allowed to see.")
            return
        plan = build_plan(application)
    except Exception as e:  # noqa: BLE001 - report any ArgoCD problem back to Slack
        audit("plan_failed", user=user, app=name, error=str(e))
        respond(f"Couldn't read `{name}` from ArgoCD: {e}")
        return
    if plan["sync_status"] == "Synced" and plan["live_images"] == plan["target_images"]:
        respond(f"`{name}` is already up to date ({code_list(plan['live_images'])}).")
        return
    payload = {
        "app": name,
        "revisions": plan["revisions"],
        "multi_source": plan["multi_source"],
        "images": plan["target_images"],
        "requester": user,
        "expires": int(time.time()) + CONFIRM_TTL_SECONDS,
    }
    client.chat_postMessage(
        channel=channel, text=f"Deploy {name}?",
        blocks=confirm_blocks(plan, user, json.dumps(payload)),
    )
    audit("deploy_requested", user=user, channel=channel, app=name,
          revisions=plan["revisions"], images=plan["target_images"])


@app.action("deploy_cancel")
def deploy_cancel(ack, body, client, respond):
    ack()
    user = body["user"]["id"]
    payload = json.loads(body["actions"][0]["value"])
    if user != payload["requester"]:
        respond(text="Only the person who ran /deploy can cancel it.",
                response_type="ephemeral", replace_original=False)
        return
    close_message(client, body, f"Cancelled by <@{user}>.")
    audit("deploy_cancelled", user=user, app=payload["app"])


@app.action("deploy_confirm")
def deploy_confirm(ack, body, client, respond):
    ack()
    user, channel = body["user"]["id"], body["channel"]["id"]
    payload = json.loads(body["actions"][0]["value"])
    name = payload["app"]
    if user != payload["requester"] or not allowed(user, channel):
        audit("confirm_denied", user=user, channel=channel, app=name)
        respond(text="Only the person who ran /deploy can confirm it.",
                response_type="ephemeral", replace_original=False)
        return
    if time.time() > payload["expires"]:
        close_message(client, body, "This request expired. Run `/deploy` again.")
        return
    with in_flight_lock:
        if name in in_flight:
            respond(text=f"`{name}` is already being deployed.",
                    response_type="ephemeral", replace_original=False)
            return
        in_flight.add(name)
    close_message(client, body, f"Confirmed by <@{user}>. Deploying...")
    threading.Thread(
        target=run_deploy, args=(client, channel, body["message"]["ts"], payload, user), daemon=True,
    ).start()


def run_deploy(client, channel, thread_ts, payload, user):
    name = payload["app"]

    def say(text):
        client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text)

    try:
        started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        start_sync(name, payload["revisions"], payload["multi_source"])
        audit("sync_started", user=user, app=name, revisions=payload["revisions"])
        say(f"Sync started in ArgoCD. Watching the rollout (timeout {SYNC_TIMEOUT_SECONDS // 60} min)...")
        ok, detail = wait_for_rollout(name, started_at, payload["images"])
        audit("deploy_finished", user=user, app=name, ok=ok, detail=detail)
        icon = ":white_check_mark:" if ok else ":x:"
        say(f"{icon} `{name}`: {detail}")
    except Exception as e:  # noqa: BLE001 - every failure must reach the thread
        audit("deploy_error", user=user, app=name, error=str(e))
        say(f":x: `{name}`: {e}")
    finally:
        with in_flight_lock:
            in_flight.discard(name)


@app.command("/deploy-status")
def status_command(ack, command, respond):
    ack()
    name = command.get("text", "").strip()
    if command["channel_id"] not in ALLOWED_CHANNELS:
        respond("This command only works in the deploy channel.")
        return
    if not APP_NAME.fullmatch(name):
        respond("Usage: `/deploy-status <application-name>`")
        return
    application = get_app(name, refresh=True)
    if application is None:
        respond(f"I can't find an application called `{name}` that I'm allowed to see.")
        return
    status = application["status"]
    operation = status.get("operationState") or {}
    respond("\n".join([
        f"*`{name}`*: {status.get('sync', {}).get('status')}, {status.get('health', {}).get('status')}",
        f"Running: {code_list(sorted(status.get('summary', {}).get('images', [])))}",
        f"Last sync: {operation.get('phase', 'never')} at {operation.get('finishedAt', '-')}"
        f" by {operation.get('operation', {}).get('initiatedBy', {}).get('username', '-')}",
    ]))


if __name__ == "__main__":
    audit("bot_started", argocd=ARGOCD_URL,
          allowed_users=len(ALLOWED_USERS), allowed_channels=len(ALLOWED_CHANNELS))
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()

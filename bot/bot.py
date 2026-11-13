"""Slack /deploy bot for ArgoCD (test prototype).

Only allowlisted Slack users, in allowlisted channels, can deploy. The bot only
syncs what is already merged in Git: it never picks images or edits values, and
every sync is pinned to the exact revisions shown in the confirmation message.
"""

import calendar
import json
import os
import re
import threading
import time

import requests
import urllib3
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler


def load_ssm_parameters():
    """Read settings from SSM Parameter Store once, at startup.

    Every parameter under SSM_PARAMETER_PATH (e.g. /slack-deploy-bot/) becomes an environment
    variable named after the last part of its name; SecureStrings are decrypted. Values are only
    read here, so restart the pod to pick up changes. Without SSM_PARAMETER_PATH, plain
    environment variables are used as they are.
    """
    path = os.environ.get("SSM_PARAMETER_PATH")
    if not path:
        return []
    import boto3

    names = []
    pages = boto3.client("ssm").get_paginator("get_parameters_by_path")
    for page in pages.paginate(Path=path, WithDecryption=True):
        for parameter in page["Parameters"]:
            name = parameter["Name"].rsplit("/", 1)[-1]
            os.environ[name] = parameter["Value"]
            names.append(name)
    return sorted(names)


LOADED_FROM_SSM = load_ssm_parameters()

ARGOCD_URL =os.environ.get("ARGOCD_URL", "https://argocd-server.argocd.svc").rstrip("/")
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


def image_repo(image):
    name = image.split("@", 1)[0]
    head, _, tail = name.rpartition(":")
    return head if head and "/" not in tail else name


def image_tag(image):
    """Just the version part of an image reference: its tag, or a short digest."""
    if "@" in image:
        return image.split("@", 1)[1][:19]
    repo = image_repo(image)
    return image[len(repo) + 1:] if len(image) > len(repo) else "latest"


def versions(images):
    """Image tags for display; the image name is added only when there's more than one image."""
    if not images:
        return "_nothing running_"
    if len(images) == 1:
        return f"`{image_tag(images[0])}`"
    return "\n".join(f"{image_repo(i).rsplit('/', 1)[-1]} `{image_tag(i)}`" for i in images)


def duration(seconds):
    seconds = int(seconds)
    return f"{seconds // 60}m {seconds % 60:02d}s" if seconds >= 60 else f"{seconds}s"


def slack_time(unix, fallback, fmt="{date_short_pretty} at {time}"):
    """A timestamp Slack shows in each reader's own time zone."""
    return f"<!date^{int(unix)}^{fmt}|{fallback}>"


def images_match(live, expected):
    """Every expected image is running, and no other tag of those images is left behind.

    Live images can include extras (chart test hooks, sidecars), so this isn't an equality check.
    """
    repos = {image_repo(i) for i in expected}
    stale = [i for i in live if image_repo(i) in repos and i not in expected]
    return set(expected) <= set(live) and not stale


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
        if phase == "Succeeded" and health == "Healthy" and images_match(live, expected_images):
            return True, "synced, healthy, and running the expected images"
    minutes = SYNC_TIMEOUT_SECONDS // 60
    return False, f"timed out after {minutes} min (sync: {phase}, health: {health})"


# --- Slack messages -------------------------------------------------------------


STATE_EMOJI = {
    "Synced": ":large_green_circle:", "Healthy": ":large_green_circle:",
    "OutOfSync": ":large_yellow_circle:", "Progressing": ":large_blue_circle:",
    "Degraded": ":red_circle:", "Missing": ":white_circle:",
}


def state(value):
    return f"{STATE_EMOJI.get(value, ':white_circle:')} {value}"


def mrkdwn(text):
    return {"type": "mrkdwn", "text": text}


def header(text):
    return {"type": "header", "text": {"type": "plain_text", "text": text[:150], "emoji": True}}


def context(text):
    return {"type": "context", "elements": [mrkdwn(text)]}


def request_blocks(payload, title, footer, buttons=False):
    """A deploy request: title, application and versions, a one-line status, and buttons while open."""
    blocks = [
        header(title),
        {"type": "section", "fields": [
            mrkdwn(f"*Application*\n`{payload['app']}`"),
            mrkdwn(f"*Requested by*\n<@{payload['requester']}>"),
            mrkdwn(f"*Running now*\n{versions(payload['live'])}"),
            mrkdwn(f"*Will deploy*\n{versions(payload['images'])}"),
        ]},
        context(footer),
    ]
    if buttons:
        value = json.dumps(payload)
        blocks.append({"type": "actions", "elements": [
            {"type": "button", "action_id": "deploy_confirm", "style": "primary",
             "text": {"type": "plain_text", "text": "Deploy"}, "value": value},
            {"type": "button", "action_id": "deploy_cancel",
             "text": {"type": "plain_text", "text": "Cancel"}, "value": value},
        ]})
    return blocks


def update_request(client, channel, ts, payload, title, footer):
    """Rewrite the request message in place, without buttons, to show where the deploy stands."""
    client.chat_update(channel=channel, ts=ts, text=title,
                       blocks=request_blocks(payload, title, footer))


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
    if plan["sync_status"] == "Synced" and images_match(plan["live_images"], plan["target_images"]):
        respond(f":white_check_mark: `{name}` is already up to date, running {versions(plan['live_images'])}.")
        return
    payload = {
        "app": name,
        "revisions": plan["revisions"],  # pinned for the sync, not shown
        "multi_source": plan["multi_source"],
        "live": plan["live_images"],
        "images": plan["target_images"],
        "requester": user,
        "expires": int(time.time()) + CONFIRM_TTL_SECONDS,
    }
    footer = (f"ArgoCD: {state(plan['sync_status'])}  ·  {state(plan['health'])}"
              f"   |   Only <@{user}> can confirm, until "
              f"{slack_time(payload['expires'], 'in 5 minutes', '{time}')}")
    client.chat_postMessage(
        channel=channel, text=f"Deploy {name}?",
        blocks=request_blocks(payload, f":rocket:  Deploy {name}?", footer, buttons=True),
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
    update_request(client, body["channel"]["id"], body["message"]["ts"], payload,
                   f":no_entry_sign:  Deploy cancelled: {payload['app']}",
                   f"Cancelled by <@{user}>. Nothing was deployed.")
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
    message_ts = body["message"]["ts"]
    if time.time() > payload["expires"]:
        update_request(client, channel, message_ts, payload,
                       f":hourglass:  Deploy request expired: {name}",
                       f"Nothing was deployed. Run `/deploy {name}` again.")
        return
    with in_flight_lock:
        if name in in_flight:
            respond(text=f"`{name}` is already being deployed.",
                    response_type="ephemeral", replace_original=False)
            return
        in_flight.add(name)
    update_request(client, channel, message_ts, payload,
                   f":hourglass_flowing_sand:  Deploying {name}",
                   f"Confirmed by <@{user}>  ·  syncing in ArgoCD, progress in the thread")
    threading.Thread(
        target=run_deploy, args=(client, channel, message_ts, payload, user), daemon=True,
    ).start()


def run_deploy(client, channel, message_ts, payload, user):
    name = payload["app"]
    clock = time.monotonic()

    def say(text):
        client.chat_postMessage(channel=channel, thread_ts=message_ts, text=text)

    def finish(ok, detail):
        took = duration(time.monotonic() - clock)
        if ok:
            update_request(client, channel, message_ts, payload, f":white_check_mark:  Deployed {name}",
                           f"Confirmed by <@{user}>  ·  healthy after {took}")
            say(f":white_check_mark: `{name}` is running {versions(payload['images'])} and healthy ({took}).")
        else:
            update_request(client, channel, message_ts, payload, f":x:  Deploy failed: {name}",
                           f"Confirmed by <@{user}>  ·  failed after {took}, details in the thread")
            say(f":x: `{name}`: {detail}")

    try:
        started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        start_sync(name, payload["revisions"], payload["multi_source"])
        audit("sync_started", user=user, app=name, revisions=payload["revisions"])
        say(f"Syncing `{name}` in ArgoCD. I'll post here when it's done "
            f"(timeout {SYNC_TIMEOUT_SECONDS // 60} min).")
        ok, detail = wait_for_rollout(name, started_at, payload["images"])
        audit("deploy_finished", user=user, app=name, ok=ok, detail=detail)
        finish(ok, detail)
    except Exception as e:  # noqa: BLE001 - every failure must reach Slack
        audit("deploy_error", user=user, app=name, error=str(e))
        finish(False, str(e))
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
    sync = status.get("sync", {}).get("status", "Unknown")
    health = status.get("health", {}).get("status", "Unknown")
    operation = status.get("operationState") or {}
    last = "_never_"
    if operation.get("finishedAt"):
        finished = calendar.timegm(time.strptime(operation["finishedAt"], "%Y-%m-%dT%H:%M:%SZ"))
        by = operation.get("operation", {}).get("initiatedBy", {}).get("username", "automated")
        last = f"{operation.get('phase', '')} {slack_time(finished, operation['finishedAt'])}\nby {by}"
    respond(text=f"{name}: {sync}, {health}", blocks=[
        header(f":mag:  {name}"),
        {"type": "section", "fields": [
            mrkdwn(f"*Sync*\n{state(sync)}"),
            mrkdwn(f"*Health*\n{state(health)}"),
            mrkdwn(f"*Running*\n{versions(sorted(status.get('summary', {}).get('images', [])))}"),
            mrkdwn(f"*Last deploy*\n{last}"),
        ]},
    ])


if __name__ == "__main__":
    audit("bot_started", argocd=ARGOCD_URL, loaded_from_ssm=LOADED_FROM_SSM,
          allowed_users=len(ALLOWED_USERS), allowed_channels=len(ALLOWED_CHANNELS))
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()

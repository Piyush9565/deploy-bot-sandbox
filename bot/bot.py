"""Slack /deploy bot for ArgoCD (test prototype).

Only allowlisted Slack users, in allowlisted channels, can deploy. The bot only
syncs what is already merged in Git: it never picks images or edits values, and
every sync is pinned to the exact revisions shown in the confirmation message.
"""

import calendar
import difflib
import json
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

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
MAX_APPS_PER_REQUEST = 10
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


def parse_app_names(text):
    """App names from a command, separated by spaces or commas: lowercased, underscores as dashes."""
    names = []
    for part in re.split(r"[\s,]+", text.strip().lower()):
        name = part.replace("_", "-")
        if name and name not in names:
            names.append(name)
    return names


def suggest(typed, names, limit=3):
    """App names closest to a typo: similar spelling of the whole name or of one of its words
    ("buyr" -> "buyer-service"), boosted when one name contains the other.

    Only names nearly as close as the best match are kept, so a shared suffix like "-service"
    doesn't drag in unrelated apps.
    """
    def similarity(a, b):
        return difflib.SequenceMatcher(None, a, b).ratio()

    def score(name):
        whole = similarity(typed, name)
        word = max(similarity(typed, part) for part in name.split("-")) - 0.1
        contained = len(typed) >= 3 and (typed in name or name in typed)
        return max(whole, word) + (0.5 if contained else 0)

    scored = sorted(((score(n), n) for n in names), key=lambda s: (-s[0], s[1]))
    if not scored or scored[0][0] < 0.6:
        return []
    best = scored[0][0]
    return [n for s, n in scored if s >= max(0.6, best - 0.15)][:limit]


def app_list(names, limit=25):
    if not names:
        return "_none_"
    more = f" and {len(names) - limit} more" if len(names) > limit else ""
    return ", ".join(f"`{n}`" for n in names[:limit]) + more


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


def list_apps():
    """(name, project) for every application the bot can see."""
    r = argocd.get(f"{ARGOCD_URL}/api/v1/applications",
                   params={"fields": "items.metadata.name,items.spec.project"}, timeout=60)
    r.raise_for_status()
    return [(a["metadata"]["name"], (a.get("spec") or {}).get("project", ""))
            for a in r.json().get("items") or []]


can_sync_cache = {}


def can_sync(project, name="*"):
    """Whether the bot's ArgoCD account may sync this app (answers cached for 5 minutes)."""
    key = (project, name)
    cached = can_sync_cache.get(key)
    if cached and time.time() - cached[1] < 300:
        return cached[0]
    r = argocd.get(f"{ARGOCD_URL}/api/v1/account/can-i/applications/sync/{project}/{name}", timeout=20)
    r.raise_for_status()
    answer = r.json().get("value") == "yes"
    can_sync_cache[key] = (answer, time.time())
    return answer


def deployable_apps():
    """Names of the apps the bot is allowed to deploy (checked per ArgoCD project)."""
    return sorted(name for name, project in list_apps() if project and can_sync(project))


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


# Labels for a single app's old and new versions, so they read right at each stage of a request.
LABELS = {
    "open": ("Running now", "Will deploy"),
    "deploying": ("Running now", "Deploying"),
    "deployed": ("Previous version", "Now running"),
    "failed": ("Before", "Attempted"),
    "not_deployed": ("Running now", "Not deployed"),
}


def app_line(entry, phase, result):
    """One app's versions in a multi-app request, with its outcome once known."""
    old, new = versions(entry["live"]), versions(entry["images"])
    if result is True:
        return f":white_check_mark: {new}  _(was {old})_"
    if result is False:
        return f":x: {new} failed  _(was {old})_"
    if phase == "deploying":
        return f":hourglass_flowing_sand: {old} → {new}"
    if phase == "not_deployed":
        return f"{old}  _(not deployed: {new})_"
    return f"{old} → {new}"


def request_blocks(request, title, footer, phase, results=None, buttons=False):
    """A deploy request: title, apps and versions, a one-line status, and buttons while open.

    One app gets the two-column card; several apps get one row each, with their own outcome.
    """
    results = results or {}
    apps = request["apps"]
    blocks = [header(title)]
    if len(apps) == 1:
        entry = apps[0]
        result = results.get(entry["app"])
        label_key = phase if result is None else ("deployed" if result else "failed")
        old_label, new_label = LABELS[label_key]
        blocks.append({"type": "section", "fields": [
            mrkdwn(f"*Application*\n`{entry['app']}`"),
            mrkdwn(f"*Requested by*\n<@{request['requester']}>"),
            mrkdwn(f"*{old_label}*\n{versions(entry['live'])}"),
            mrkdwn(f"*{new_label}*\n{versions(entry['images'])}"),
        ]})
    else:
        blocks.append(context(f"Requested by <@{request['requester']}>  ·  {len(apps)} apps"))
        for start in range(0, len(apps), 4):  # a section holds 10 fields: a header row + 4 apps
            fields = [mrkdwn("*Application*"), mrkdwn("*Version*")]
            for entry in apps[start:start + 4]:
                fields += [mrkdwn(f"`{entry['app']}`"),
                           mrkdwn(app_line(entry, phase, results.get(entry["app"])))]
            blocks.append({"type": "section", "fields": fields})
    if request.get("skipped"):
        blocks.append(context(f"Already up to date, not included: {app_list(request['skipped'])}"))
    blocks.append(context(footer))
    if buttons:
        blocks.append({"type": "actions", "elements": [
            {"type": "button", "action_id": "deploy_confirm", "style": "primary",
             "text": {"type": "plain_text", "text": "Deploy" if len(apps) == 1 else f"Deploy {len(apps)} apps"},
             "value": request["id"]},
            {"type": "button", "action_id": "deploy_cancel",
             "text": {"type": "plain_text", "text": "Cancel"}, "value": request["id"]},
        ]})
    return blocks


def update_request(client, channel, ts, request, title, footer, phase, results=None):
    """Rewrite the request message in place, without buttons, to show where the deploy stands."""
    client.chat_update(channel=channel, ts=ts, text=title,
                       blocks=request_blocks(request, title, footer, phase, results))


def subject(request):
    """How a request is named in titles: the app, or "N apps"."""
    apps = request["apps"]
    return apps[0]["app"] if len(apps) == 1 else f"{len(apps)} apps"


def suggestion_blocks(typed, matches, action_prefix, verb):
    """Private 'did you mean' message with a button for each close match."""
    buttons = [{"type": "button", "action_id": f"{action_prefix}_{i}", "value": name,
                "text": {"type": "plain_text", "text": f"{verb} {name}"[:75]}}
               for i, name in enumerate(matches)]
    return [
        {"type": "section", "text": mrkdwn(f":mag: I couldn't find `{typed}`. Did you mean:")},
        {"type": "actions", "elements": buttons},
    ]


# Open deploy requests, keyed by the id in their buttons. Kept in memory: after a bot restart,
# old buttons just say the request is gone.
requests_by_id = {}
requests_lock = threading.Lock()


def save_request(request):
    with requests_lock:
        cutoff = time.time() - 3600
        for request_id in [r for r, old in requests_by_id.items() if old["expires"] < cutoff]:
            del requests_by_id[request_id]
        requests_by_id[request["id"]] = request


def take_request(request_id, user, new_status):
    """Move an open request to a new status for its requester. Returns (request, problem)."""
    with requests_lock:
        request = requests_by_id.get(request_id)
        if request is None:
            return None, "This request is no longer available (the bot may have restarted). Run `/deploy` again."
        if user != request["requester"]:
            return None, "Only the person who ran /deploy can confirm or cancel it."
        if request["status"] != "open":
            return None, f"This request is already {request['status']}."
        request["status"] = new_status
        return request, None


# --- Slack handlers --------------------------------------------------------------


@app.command("/deploy")
def deploy_command(ack, command, respond, client):
    ack()
    user, channel = command["user_id"], command["channel_id"]
    names = parse_app_names(command.get("text", ""))
    if not allowed(user, channel):
        audit("deploy_denied", user=user, channel=channel, apps=names)
        respond("You're not allowed to deploy from here.")
        return
    if not names:
        usage = "Usage: `/deploy <app> [more apps…]`, e.g. `/deploy seller-offer-service buyer-service`"
        try:
            respond(f"{usage}\nApps you can deploy: {app_list(deployable_apps())}")
        except Exception as e:  # noqa: BLE001 - report any ArgoCD problem back to Slack
            respond(f"{usage}\n(couldn't list apps from ArgoCD: {e})")
        return
    request_deploy(names, user, channel, client, respond)


@app.action(re.compile(r"suggest_deploy_\d+"))
def suggest_deploy(ack, body, client, respond):
    """A 'did you mean' button: carry on as if the suggested name had been typed."""
    ack()
    user, channel = body["user"]["id"], body["channel"]["id"]
    if not allowed(user, channel):
        respond(text="You're not allowed to deploy from here.", replace_original=True)
        return
    respond(delete_original=True)
    request_deploy([body["actions"][0]["value"]], user, channel, client, respond)


def check_app(name):
    """Where one requested app stands: ("ready" | "up_to_date", plan), ("not_permitted", project),
    or ("not_found", None)."""
    application = get_app(name, refresh=True) if APP_NAME.fullmatch(name) else None
    if application is None:
        return "not_found", None
    project = application["spec"]["project"]
    if not can_sync(project, name):
        return "not_permitted", project
    plan = build_plan(application)
    if plan["sync_status"] == "Synced" and images_match(plan["live_images"], plan["target_images"]):
        return "up_to_date", plan
    return "ready", plan


def request_deploy(names, user, channel, client, respond):
    """Post one deploy request for the apps, or explain privately why not (with suggestions on typos)."""
    if len(names) > MAX_APPS_PER_REQUEST:
        respond(f"I can deploy up to {MAX_APPS_PER_REQUEST} apps at once; you listed {len(names)}.")
        return
    try:
        with ThreadPoolExecutor(max_workers=len(names)) as pool:
            checks = dict(zip(names, pool.map(check_app, names)))
        not_found = [n for n, (status, _) in checks.items() if status == "not_found"]
        not_permitted = {n: project for n, (status, project) in checks.items() if status == "not_permitted"}
        deployable = deployable_apps() if not_found else []
    except Exception as e:  # noqa: BLE001 - report any ArgoCD problem back to Slack
        audit("plan_failed", user=user, apps=names, error=str(e))
        respond(f"Couldn't read the app{'s' if len(names) > 1 else ''} from ArgoCD: {e}")
        return

    if len(names) == 1 and not_found:
        name = names[0]
        matches = suggest(name, deployable)
        audit("app_not_found", user=user, app=name, suggested=matches)
        if matches:
            respond(text=f"I couldn't find {name}.",
                    blocks=suggestion_blocks(name, matches, "suggest_deploy", "Deploy"))
        else:
            respond(f":mag: I couldn't find `{name}`. Apps you can deploy: {app_list(deployable)}")
        return
    if len(names) == 1 and not_permitted:
        name, project = next(iter(not_permitted.items()))
        audit("deploy_not_permitted", user=user, app=name, project=project)
        respond(f":no_entry: `{name}` is in ArgoCD project `{project}`, which this bot isn't allowed to deploy.")
        return
    if not_found or not_permitted:
        lines = []
        for name in not_found:
            matches = suggest(name, deployable)
            lines.append(f"• `{name}` not found" + (f". Did you mean {app_list(matches)}?" if matches else "."))
        for name, project in not_permitted.items():
            lines.append(f"• `{name}` is in ArgoCD project `{project}`, which this bot isn't allowed to deploy.")
        audit("deploy_request_rejected", user=user, not_found=not_found, not_permitted=list(not_permitted))
        respond(":warning: I didn't create a deploy request. Fix these and run `/deploy` again:\n" + "\n".join(lines))
        return

    ready = [(n, plan) for n, (status, plan) in checks.items() if status == "ready"]
    skipped = [n for n, (status, _) in checks.items() if status == "up_to_date"]
    if not ready:
        respond(":white_check_mark: Already up to date: " + ", ".join(
            f"`{n}` {versions(checks[n][1]['live_images'])}" for n in names))
        return

    request = {
        "id": uuid.uuid4().hex[:12],
        "requester": user,
        "expires": int(time.time()) + CONFIRM_TTL_SECONDS,
        "status": "open",
        "skipped": skipped,
        "apps": [{
            "app": n,
            "revisions": plan["revisions"],  # pinned for the sync, not shown
            "multi_source": plan["multi_source"],
            "live": plan["live_images"],
            "images": plan["target_images"],
        } for n, plan in ready],
    }
    save_request(request)
    until = f"Only <@{user}> can confirm, until {slack_time(request['expires'], 'in 5 minutes', '{time}')}"
    if len(ready) == 1:
        plan = ready[0][1]
        footer = f"ArgoCD: {state(plan['sync_status'])}  ·  {state(plan['health'])}   |   {until}"
    else:
        footer = f"All {len(ready)} apps deploy at the same time   |   {until}"
    title = f":rocket:  Deploy {subject(request)}?"
    client.chat_postMessage(channel=channel, text=title,
                            blocks=request_blocks(request, title, footer, "open", buttons=True))
    audit("deploy_requested", user=user, channel=channel, request=request["id"],
          apps={a["app"]: {"revisions": a["revisions"], "images": a["images"]} for a in request["apps"]},
          skipped=skipped)


@app.action("deploy_cancel")
def deploy_cancel(ack, body, client, respond):
    ack()
    user = body["user"]["id"]
    request, problem = take_request(body["actions"][0]["value"], user, "cancelled")
    if problem:
        respond(text=problem, response_type="ephemeral", replace_original=False)
        return
    update_request(client, body["channel"]["id"], body["message"]["ts"], request,
                   f":no_entry_sign:  Deploy cancelled: {subject(request)}",
                   f"Cancelled by <@{user}>. Nothing was deployed.", "not_deployed")
    audit("deploy_cancelled", user=user, request=request["id"], apps=[a["app"] for a in request["apps"]])


@app.action("deploy_confirm")
def deploy_confirm(ack, body, client, respond):
    ack()
    user, channel = body["user"]["id"], body["channel"]["id"]
    if not allowed(user, channel):
        audit("confirm_denied", user=user, channel=channel)
        respond(text="You're not allowed to deploy from here.", response_type="ephemeral", replace_original=False)
        return
    request, problem = take_request(body["actions"][0]["value"], user, "deploying")
    if problem:
        audit("confirm_denied", user=user, channel=channel, reason=problem)
        respond(text=problem, response_type="ephemeral", replace_original=False)
        return
    message_ts = body["message"]["ts"]
    names = [a["app"] for a in request["apps"]]
    if time.time() > request["expires"]:
        request["status"] = "expired"
        update_request(client, channel, message_ts, request,
                       f":hourglass:  Deploy request expired: {subject(request)}",
                       f"Nothing was deployed. Run `/deploy {' '.join(names)}` again.", "not_deployed")
        return
    with in_flight_lock:
        busy = [n for n in names if n in in_flight]
        if not busy:
            in_flight.update(names)
    if busy:
        request["status"] = "open"
        respond(text=f"Already being deployed: {app_list(busy)}. Try again when that finishes.",
                response_type="ephemeral", replace_original=False)
        return
    update_request(client, channel, message_ts, request,
                   f":hourglass_flowing_sand:  Deploying {subject(request)}",
                   f"Confirmed by <@{user}>  ·  syncing in ArgoCD, progress in the thread", "deploying")
    threading.Thread(
        target=run_deploys, args=(client, channel, message_ts, request, user), daemon=True,
    ).start()


def run_deploys(client, channel, message_ts, request, user):
    """Sync every app in the request at the same time, reporting each result in the thread."""
    apps = request["apps"]
    multi = len(apps) > 1
    clock = time.monotonic()
    results = {}
    results_lock = threading.Lock()

    def say(text):
        client.chat_postMessage(channel=channel, thread_ts=message_ts, text=text)

    def deploy_one(entry):
        name = entry["app"]
        app_clock = time.monotonic()
        try:
            started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            start_sync(name, entry["revisions"], entry["multi_source"])
            audit("sync_started", user=user, app=name, revisions=entry["revisions"], request=request["id"])
            ok, detail = wait_for_rollout(name, started_at, entry["images"])
        except Exception as e:  # noqa: BLE001 - every failure must reach Slack
            ok, detail = False, str(e)
        took = duration(time.monotonic() - app_clock)
        audit("deploy_finished", user=user, app=name, ok=ok, detail=detail, request=request["id"])
        with in_flight_lock:
            in_flight.discard(name)
        if ok:
            say(f":white_check_mark: `{name}` is running {versions(entry['images'])} and healthy ({took}).")
        else:
            say(f":x: `{name}`: {detail}")
        with results_lock:
            results[name] = ok
            snapshot = dict(results)
        if multi and len(snapshot) < len(apps):  # live progress on the request message
            update_request(client, channel, message_ts, request,
                           f":hourglass_flowing_sand:  Deploying {subject(request)}",
                           f"Confirmed by <@{user}>  ·  {len(snapshot)} of {len(apps)} finished",
                           "deploying", snapshot)

    if multi:
        say(f"Syncing {len(apps)} apps in ArgoCD at the same time: {app_list([a['app'] for a in apps])}. "
            f"I'll post each result here (timeout {SYNC_TIMEOUT_SECONDS // 60} min each).")
    else:
        say(f"Syncing `{apps[0]['app']}` in ArgoCD. I'll post here when it's done "
            f"(timeout {SYNC_TIMEOUT_SECONDS // 60} min).")
    try:
        with ThreadPoolExecutor(max_workers=len(apps)) as pool:
            list(pool.map(deploy_one, apps))
    finally:
        with in_flight_lock:
            in_flight.difference_update(a["app"] for a in apps)

    took = duration(time.monotonic() - clock)
    succeeded = sum(1 for ok in results.values() if ok)
    request["status"] = "finished"
    if succeeded == len(apps):
        title = f":white_check_mark:  Deployed {subject(request)}"
        footer = f"Confirmed by <@{user}>  ·  healthy after {took}"
    elif succeeded:
        title = f":warning:  Deployed {succeeded} of {len(apps)} apps"
        footer = f"Confirmed by <@{user}>  ·  finished after {took}, failures in the thread"
    else:
        title = f":x:  Deploy failed: {subject(request)}"
        footer = f"Confirmed by <@{user}>  ·  failed after {took}, details in the thread"
    update_request(client, channel, message_ts, request, title, footer, "done", results)
    audit("request_finished", user=user, request=request["id"], succeeded=succeeded, total=len(apps))


@app.command("/deploy-status")
def status_command(ack, command, respond):
    ack()
    names = parse_app_names(command.get("text", ""))
    if command["channel_id"] not in ALLOWED_CHANNELS:
        respond("This command only works in the deploy channel.")
        return
    if not names:
        respond(f"Usage: `/deploy-status <app> [more apps…]`\nApps: {app_list(sorted(n for n, _ in list_apps()))}")
        return
    for name in names[:MAX_APPS_PER_REQUEST]:
        show_status(name, respond)


@app.action(re.compile(r"suggest_status_\d+"))
def suggest_status(ack, body, respond):
    """A 'did you mean' button on /deploy-status: show the suggested app instead."""
    ack()
    if body["channel"]["id"] not in ALLOWED_CHANNELS:
        respond(text="This command only works in the deploy channel.", replace_original=True)
        return
    respond(delete_original=True)
    show_status(body["actions"][0]["value"], respond)


def show_status(name, respond):
    application = get_app(name, refresh=True) if APP_NAME.fullmatch(name) else None
    if application is None:
        names = sorted(n for n, _ in list_apps())
        matches = suggest(name, names)
        if matches:
            respond(text=f"I couldn't find {name}.",
                    blocks=suggestion_blocks(name, matches, "suggest_status", "Status of"))
        else:
            respond(f":mag: I couldn't find `{name}`. Apps: {app_list(names)}")
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

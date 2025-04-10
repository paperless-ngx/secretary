import asyncio
import os
import sys
import traceback

import aiohttp
from blacksheep import Application, Response
import cachetools
from gidgethub import aiohttp as gh_aiohttp
from gidgethub import routing
from gidgethub import sansio
from gidgethub.apps import get_installation_access_token

from unidiff import PatchSet

router = routing.Router()
cache = cachetools.LRUCache(maxsize=500)


def get_responsible_teams(diff):
    responsible = [
        ("src/", "backend"),
        ("requirements.txt", "backend"),
        ("src-ui/", "frontend"),
        ("docs/", "documentation"),
        (".github/", "ci-cd")
    ]
    teams = set()
    for change in diff:
        for responsibility, team in responsible:
            if change.path.startswith(responsibility):
                teams.add(team)
    return teams


def get_change_size(diff):
    size = 0
    ignore_types = ["rst", "md", "txt", "lock"]
    for change in diff:
        if change.path.split(".")[-1] not in ignore_types:
            for hunk in change:
                for line in hunk:
                    # Ignore whitespace and single-char lines ('{' etc.)
                    if line.is_added and len(line.value.strip()) > 2:
                        print(f">OOOO{size} {line}")
                        size += 1
    return size


new_pr_template = r"""
Hello @{user},

Thank you very much for submitting this PR to us!

This is what will happen next:

1. CI tests will run against your PR to ensure quality and consistency.
2. Next, human contributors from paperless-ngx review your changes. {review_conditions}
3. Please address any issues that come up during the review as soon as you are able to.
4. If accepted, your pull request will be merged into the `dev` branch and changes there will be tested further.
5. Eventually, changes from you and other contributors will be merged into `main` and a new release will be made.

You'll be hearing from us soon, and thank you again for contributing to our project.
"""


@router.register("pull_request", action="opened")
async def opened_pr(event, gh, *arg, **kwargs):
    """Mark new PRs as needing a review."""
    pull_request = event.data["pull_request"]

    async with aiohttp.ClientSession() as session:
        async with session.get(pull_request["patch_url"]) as resp:
            patch = PatchSet(await resp.text())

    members = await gh.getitem("/orgs/paperless-ngx/members")
    members = [m["login"] for m in members]

    user = pull_request["user"]["login"]
    if "github-actions" in user:
        print(f"ignoring PR from {user}")
        return

    labels = []
    small_change = get_change_size(patch) < 10
    responsible = get_responsible_teams(patch)

    is_dependency_pr = "dependabot" in user
    is_translation_pr = "paperlessngx-bot" in user

    if small_change:
        labels += ["small-change"]
    else:
        labels += ["non-trivial"]

    if is_dependency_pr:
        labels = ["dependencies"]
    
    labels += responsible

    if is_translation_pr:
        labels = ["skip-changelog", "translation"]

    await gh.post(pull_request["issue_url"] + "/labels", data=labels)

    if is_dependency_pr or is_translation_pr:
        print(f"ignoring comment for auto-generated PR")
        return

    if user in members:
        print("Ignoring comment for org members")
        return

    if small_change:
        review_conditions = "Since this seems to be a small change, only a single contributor has to review your changes."
    else:
        review_conditions = "Since this is a non-trivial change, a review from at least two contributors is required."

    comment = new_pr_template.format(user=user, review_conditions=review_conditions)
    print(pull_request["comments_url"], {"body": comment})
    await gh.post(pull_request["comments_url"], data={"body": comment})


app = Application()


@app.router.post("/api/pr")
async def main(request):
    try:
        async with aiohttp.ClientSession() as session:
            access_token_response = await get_installation_access_token(
                gh=gh_aiohttp.GitHubAPI(session, "paperless-ngx/paperless-ngx"),
                installation_id="23363758",
                app_id="173391",
                private_key=os.environ.get("PRIVATE_KEY")
            )
        body = await request.read()
        secret = os.environ.get("GH_SECRET")
        headers = {k.decode(): v.decode() for k, v in request.headers.items()}
        event = sansio.Event.from_http(headers, body, secret=secret)
        print('GH delivery ID', event.delivery_id, file=sys.stderr)
        if event.event == "ping":
            return Response(200)
        async with aiohttp.ClientSession() as session:
            gh = gh_aiohttp.GitHubAPI(session, "paperless-ngx/paperless-ngx",
                                      oauth_token=access_token_response["token"],
                                      cache=cache)
            # Give GitHub some time to reach internal consistency.
            await asyncio.sleep(1)
            await router.dispatch(event, gh)
        try:
            print('GH requests remaining:', gh.rate_limit.remaining)
        except AttributeError:
            pass
        return Response(200)
    except Exception as exc:
        traceback.print_exc(file=sys.stderr)
        return Response(500)

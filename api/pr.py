import asyncio
import importlib
import os
import sys
import traceback

import aiohttp
from aiohttp import web
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
    file_types = ["py", "ts"]
    for change in diff:
        if change.path.split(".")[-1] in file_types:
            size += len(change)
    return size

new_pr_template = r"""
Hello @{user},

thank you very much for submitting this PR to us!

This is what will happen next:

1. My robotic colleagues are currently checking your changes to see if they break anything. You can see the progress below.
2. Once that is finished, human contributors from paperless-ngx review your changes. {review_conditions} {page_reviewers}
3. Please improve anything that comes up during the review until your pull request gets approved.
4. Your pull request will be merged into the `dev` branch. Changes there will be tested further.
5. Eventually, changes from you and other contributors will be merged into `master` and a new release will be made.

Please allow up to 7 days for an initial review. We're all very excited about new pull requests but we only do this as a hobby.
If any action is required by you, please reply within a month.
"""

@router.register("pull_request", action="opened")
async def opened_pr(event, gh, *arg, **kwargs):
    """Mark new PRs as needing a review."""
    pull_request = event.data["pull_request"]
    patch = PatchSet(pull_request["patch_url"])

    labels = []
    small_change = get_change_size(patch) < 5
    responsible = get_responsible_teams(patch)


    if small_change:
        labels += ["small-change"]
    else:
        labels += ["non-trivial"]
    labels += responsible
    await gh.post(pull_request["labels_url"], data=labels)

    if small_change:
        review_conditions = "Since this seems to be only a small change, only a single contributor will have to review your changes."
    else:
        review_conditions = "Since this is a non-trivial change, a review from at least two contributors is required."
    
    page_reviewers = ", ".join(map(lambda x: f"@{x}", responsible))
    if page_reviewers:
        page_reviewers = f"Someone from {page_reviewers} should look at your PR."

    comment = new_pr_template.format(user=pull_request["user"]["login"], review_conditions=review_conditions, page_reviewers=page_reviewers)
    await gh.post(pull_request["comments_url"], data=comment)


async def main(request):
    try:
        
        access_token_response = await get_installation_access_token(
            installation_id=123,
            app_id=456,
            private_key= os.environ.get("PRIVATE_KEY")
        )
        body = await request.read()
        secret = os.environ.get("GH_SECRET")
        event = sansio.Event.from_http(request.headers, body, secret=secret)
        print('GH delivery ID', event.delivery_id, file=sys.stderr)
        if event.event == "ping":
            return web.Response(status=200)
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
        return web.Response(status=200)
    except Exception as exc:
        traceback.print_exc(file=sys.stderr)
        return web.Response(status=500)


app = web.Application()
app.router.add_post("/", main)

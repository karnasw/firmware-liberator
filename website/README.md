# The website

[firmwareliberator.com](https://firmwareliberator.com) is the repository's
README and docs as web pages, built with [Astro](https://astro.build) and
served by a Cloudflare Worker that holds nothing but the built files. The
pages are written once, as the repository's Markdown; this folder only turns
them into a site.

This page says how the pieces fit and what a person does by hand. The
specifics - a limit, a rule, a permission, a schedule - are written once,
beside the code that sets them, with the reason for each.

## The pieces

| Piece | What it is | Where it is written down |
|---|---|---|
| The site | The Markdown pages, and `/stats`, built into files and served by the Worker `firmware-liberator` | `src/`, `astro.config.mjs`, `public/`, `wrangler.jsonc` |
| The API | `api.firmwareliberator.com`: a Worker of its own, which takes the reports and readouts the tool sends | `api/` ([api/openapi.json](api/openapi.json) describes each path; [api/index.js](api/index.js) says how each is guarded) |
| The admin view | `admin.firmwareliberator.com`: the maintainer's view of what the API holds, behind Cloudflare Access | `admin/` |
| The zone and the account | DNS, redirects, rate-limiting, firewall and cache rules, the two buckets, the database, email, Access, Web Analytics | [tools/repo/cloudflare_settings.py](../tools/repo/cloudflare_settings.py) |
| The repository's side | The `website` environment, its secrets and the branch it takes | [tools/repo/github_settings.py](../tools/repo/github_settings.py) |
| Deploying and checking | The workflows that build, deploy and check all of the above | `.github/workflows/website.yml`, `cloudflare-settings.yml` |

The API is kept apart from the site so that nothing sent to it can affect
the site. How readouts are handled, and how to look after what the API holds,
is in [.github/readouts.md](../.github/readouts.md).

## Changing something

| To change | Edit | Then |
|---|---|---|
| A page | Its Markdown file | Push: the website workflow builds and deploys it |
| How pages are addressed, linked or described | `src/site.mjs` | `npm test`, push |
| A limit or a path of the API | `api/`, and `api/openapi.json` (below) | `npm test`, push; `apply` for the firewall rule, made from `openapi.json` |
| The API's database | A new file in `api/migrations/` (`api/database.js` says how one is written) | Run `apply`, then push |
| A DNS record, rule, bucket or other Cloudflare setting | `cloudflare_settings.py` | Run `apply` (below), then push |
| A secret or setting of the `website` environment | `github_settings.py` | `python3 tools/repo/github_settings.py apply`, or `... secret NAME` |
| A Cloudflare token's permissions | The dashboard, and the token's list in `cloudflare_settings.py` | Run the script's check |

### Changing the API

`api/openapi.json` describes the API (OpenAPI 3.1): each path, what it
takes, and every answer it gives. `test/openapi.test.mjs` holds the Worker to
it both ways - every answer it gives is described, every one described is
given - and the tool's own requests too; the firewall rule in front of the API
is made from its paths.

A path under `/v1/` keeps working for every version of the tool released
against it, so a change to one may only widen what it takes and narrow what
it answers (adding a field to an answer is fine: the tool ignores one it does
not know). A change that cannot goes under `/v2/`, beside `/v1/`, until no
tool still in use asks the old. `/confirm` and `/forget` are in no version:
they are links in emails, which must keep working.

Each description released is kept in `test/fixtures/openapi/`, named by its
`info.version` and never changed once out, and the test holds every later one
compatible with it. A release that changes the API raises `info.version`
and adds its description there, with its SHA-256 in the test's `WRITTEN`.

## Pages and addresses

Every Markdown file git tracks is a page, except those under `.github/`. A
page's address is its path without `.md`: `docs/emulator.md` is
`/docs/emulator`, a folder's `README.md` is the folder, and the README is
`/`. A link to another page goes to its address, and a link to anything else
in the repository goes to it on GitHub. `src/site.mjs` holds these rules, and
`test/` tests them.

Pages under `archive/` stay readable but are kept out of search results.
`public/llms.txt` is a short guide to the site for AI tools, written by hand:
add a page to it when the page matters to someone converting a device.
`tests/repo/test_site.py` holds its list of devices, and the wording that
names every maker and firmware, to what the tool has.

`/stats` is built from the public figures the API's nightly job writes
(`src/stats.mjs`), so the website workflow also builds daily, after them. To
build the page without the live figures, make some up:

    node scripts/sample-stats.mjs > /tmp/stats.json
    STATS_FILE=/tmp/stats.json npm run build

## Seeing it before a deploy

With Docker, and nothing else installed, from the checkout:

    website/scripts/preview.sh

It builds the site in a container from your checkout as it is now,
uncommitted changes included, with `/stats` from made-up figures, and
serves it beside the admin view, which shows a made-up database
(`scripts/admin-preview.mjs`: the page only, without Access or its
buttons). It prints both addresses, reachable from your computer alone;
Ctrl-C stops it.

## Building it

Node 22 or newer:

    cd website
    npm ci --ignore-scripts
    npm test
    npm run build          # into dist/
    npm run check          # what was built, as search engines read it
    npx astro preview      # dist/ at http://localhost:4321

Packages come only from `package-lock.json`, by hash, with no install
scripts run. Dependabot moves them (`.github/dependabot.yml`), and the
dependabot-merge workflow merges its pull requests.

## Deploying it

`.github/workflows/website.yml` tests and builds the site for each pull
request. On each push to `main` it deploys the Workers, each in a job
of its own so that no deploy holds up another, and then checks what it
deployed. It runs in the `website` environment, which only `main` can use.

### The Cloudflare tokens

Each token is allowed only what its job needs, and named for that job.
`TOKENS` in `tools/repo/cloudflare_settings.py` is the list: each token, what
it is for, where its value is kept, and what it is allowed, with why. `apply`
checks every token in the account against it.

`python3 tools/repo/cloudflare_tokens.py` makes the tokens, or remakes them
with what `TOKENS` says and new values, and puts each value where it is
kept, never on the screen. Run it after changing `TOKENS`, and to replace
every token should one have been seen. A token that can make tokens could
make itself any other, so the script runs only with one you make by hand,
short-lived and usable from your computer alone, which it deletes when it is
done - or, should a step fail, keeps, so you can run it again with the same
value until it expires. Where your address changes from one request to the
next, as on a mobile connection, give each such token the network it changes
within instead, no wider than `WIDEST_FILTER` in `cloudflare_settings.py`
allows.
Run it and it says how to make that token, then asks for it, so it is
never written down. One made otherwise it leaves untouched and says what to
change, so you can edit it in the dashboard and run it again.

CI never checks the tokens' permissions, as reading one token means reading
every token in the account. It checks instead, on each run, that its own
token is refused what it must never reach.

### Changing a Cloudflare setting

Settings are applied by hand, never by CI. They change rarely, and a CI token
that can only read cannot, if it leaks, change the DNS or a bucket.

1. Edit `cloudflare_settings.py`. A record or rule made in the dashboard is
   a difference that `apply` removes, so write it down there instead.
2. Run `python3 tools/repo/cloudflare_settings.py` to see what differs, and
   `... apply` to set it, in a terminal. Each asks for the settings-apply
   token and says how to ready it in the dashboard: Roll it, then give it a
   short TTL - in that order, as an expired token's TTL cannot be changed
   until it is rolled - so each value serves one run. `apply` says what is left to do by hand,
   and writes the values Cloudflare gives that the repository holds - the
   database's id, the Access applications' audience tags, the Web Analytics
   site token - into their files, the rest of each as it was, and commits
   them as it ends, so none is left uncommitted: only those files, and only
   one that had no changes before, as another session may be editing it - one
   that had is written and left for you to commit with its changes.
3. Push.

The cloudflare-settings workflow then checks the settings daily, and on each
push that changes the script. It fails on any difference, whether made in the
dashboard or written down and not applied. It cannot check the database, the
Workers' secrets, Access or Web Analytics, which its token is refused: the
script run by hand, with the apply token, checks those.

The domain's mail forwarding - to whom, and any addresses besides those the
site publishes - is private, so it is written nowhere in the repository:
Email Routing keeps it. The published ones are `FORWARDED` in
`tools/repo/cloudflare_settings.py`, and `apply` forwards each one missing
where the others go. `python3 tools/repo/cloudflare_settings.py email`, run
in a terminal, asks to whom and for any others, and sets them, and the API's
alerts address with them; every check holds the forwarding to its shape. Put the address in
`tools/repo/leak-scan.local`, so the leak scan stops a commit that holds it.

### The admin view

Cloudflare Access stands in front of the admin view, and the Worker checks
Access's signed token on every request itself, so a request that reached it
some other way is refused. `apply` makes Access's part and says each step
that is left to you. Two are always yours:

1. A Zero Trust organisation for the account (dashboard, Zero Trust, free
   plan).
2. A GitHub OAuth app for the sign-in, with the homepage and callback
   `apply` names. `apply`, run in a terminal, asks for its client ID and a
   new secret once, to make the sign-in, and keeps neither.

The one address let in is `ACCESS_ADMIN_EMAIL` in `tools/repo/.env.infra`, or else the
one the domain's mail is forwarded to. `apply` then writes the team, the
application's audience tag and the account into `admin/wrangler.jsonc`, and
commits them; until they are deployed the Worker lets no one in. CI
does not check Access, and each deploy checks only that the view turns away a
request that is not signed in.

The emulator suites send their runs under the view's `/v1/emulator`
(`admin/emulator.js`, `docs/testing.md` Emulator stats) with an Access service
token of their own, which can do nothing else. `apply` makes the token, never
to expire, and a second Access application over that path alone that lets in
the token and no one else. It writes the token's id and secret to
`~/.config/firmware-liberator/emulator.env`, as Access shows a secret only
once, sets it as the repository's secrets `EMULATOR_STATS_CLIENT_ID` and
`EMULATOR_STATS_CLIENT_SECRET` (gh, signed in) for the final-firmware check on
`main`, and copies that file over ssh to each machine named in
`tools/repo/.env.infra` as `EMULATOR_STATS_HOSTS`. It then writes the
application's audience tag into `admin/wrangler.jsonc` as `ACCESS_UPLOAD_AUD`. Should the secret be seen,
`python3 tools/repo/cloudflare_settings.py rotate emulator-stats` gives the
token a new one, which stops the old at once, and writes and copies it the
same way. A machine added later gets it with `python3
tools/repo/emulator_stats.py push HOST`. Each deploy of the view checks that the suites' path turns
away a request without the token.

### Adding a Worker

The deploy token can only be given a Worker that already exists, so a new
one is first uploaded from your computer:

1. Ready the settings-apply token, as above. wrangler reads its own name:
   set `CLOUDFLARE_API_TOKEN` to its value for the upload.
2. Check the name is free (dashboard, Workers & Pages): an upload under a
   taken name replaces that Worker.
3. Run `apply` to make what the Worker binds to, as the upload fails on a
   binding to something that is not there. For the database it writes the id
   into each Worker's `wrangler.jsonc` and `tools/repo/leak-scan-allow`, and
   commits them, before the upload.
4. Upload it from `website/`: `npx wrangler deploy --config <its
   wrangler.jsonc>`. Or, to give the deploy token the Worker before it is
   ready, upload a placeholder under its name with the Workers API: a script
   answering 404, with no route, custom domain, `workers.dev` address or
   previews. The workflow's first deploy then replaces it.
5. Run `apply` again. It sets the Worker's secrets, which its
   `wrangler.jsonc` describes, and says what is left.
6. Check it answers (below).
7. Add it to `DEPLOY_WORKERS`, remake the deploy token
   (`cloudflare_tokens.py workers-deploy`), and give the website workflow a
   job that deploys it.

### Stopping the API

Should something need looking at, the API can stop taking requests without a
change to the repository: Actions, Website, Run workflow, with **ingest**
off. Every path then answers 503, and the tool keeps what it would have sent
to send later. This lasts until the next deploy, which any push to `main`
makes. To keep it off longer, set `INGEST` to `"off"` in `api/wrangler.jsonc`
and push that.

### Checking a deploy

The API's deploy checks itself: its last step,
`python3 tools/repo/api_health.py live`, fails the job unless the new Worker
answers as one deployed with its database and newest migration does.
Every answer names, in its `API-Tree` header, the git tree of `api/` it was
deployed from, and the release workflow releases a tool only once the API
answering is its own commit's (`api_health.py deployed`): a tool never goes
out ahead of the API it needs. A release held up there means the deploy
failed, or the database needs `cloudflare_settings.py apply` first. By
hand:

```sh
curl -sI https://firmwareliberator.com/             # 200
curl -sI https://firmwareliberator.com/liberate.sh  # 302
curl -s -o /dev/null -w '%{http_code}\n' https://api.firmwareliberator.com/v1/readout  # 403: only a POST gets through
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://api.firmwareliberator.com/v1/reports  # 415: the Worker answers
curl -s -o /dev/null -w '%{http_code}\n' https://api.firmwareliberator.com/         # 403
curl -s https://stats.firmwareliberator.com/v1/stats.json | head -c 120   # after the first night
```

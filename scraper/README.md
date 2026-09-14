# scraper

Keeps [`../licenses.txt`](../licenses.txt) in step with the Ministry of Health's
[current licence holders](https://www.health.govt.nz/regulation-legislation/medicinal-cannabis/information-for-industry/current-licence-holders)
page. Runs once a day from GitHub Actions
([`.github/workflows/scrape.yml`](../.github/workflows/scrape.yml)); no server or
personal credentials involved.

## How it works

1. **Fetch.** The page sits behind a Cloudflare *managed challenge*. Cloudflare
   decides who gets the challenge from the client's TLS fingerprint, so `curl`,
   `wget` and Python `requests` all get a `403 Just a moment...` page no matter
   which headers or User-Agent they send. [`curl_cffi`](https://github.com/lexiforest/curl_cffi)
   makes the request with a real browser's TLS fingerprint and gets the page
   straight away. The script rotates chrome / firefox / safari profiles across
   three attempts if the first is refused.
2. **Parse.** Finds the table whose header is *Name of Licensee / Expiry date*
   and the `Last updated <time datetime=...>` block in the page footer. Cell text
   is whitespace-normalised (the page sometimes contains non-breaking spaces).
3. **Compare and commit.** Rows are written to `licenses.txt` as
   `name<TAB>expiry`, in page order, exactly as the file has always been kept.
   If the file changed, `--commit` makes a commit whose subject is the page's
   *Last updated* date as `YYYYMMDD` (the repo's existing convention) and whose
   body lists the additions (`+`), removals (`-`) and renewals (`~`).
   `--push` pushes it.

A date-only change (the Ministry re-saves the page without touching the table)
does **not** produce a commit. The history stays about the licence list.

Anything unexpected in the page structure (no table, rows without exactly two
cells, fewer than 10 rows, a challenge page) aborts with exit status 1 before
anything is written. A broken fetch can therefore never be committed as a
"change"; the Actions run just goes red and GitHub emails the repo owner.

## Running it yourself

```bash
cd scraper
python3 -m venv venv && venv/bin/pip install -r requirements.txt
venv/bin/python scrape.py --dry-run          # fetch, parse, report; write nothing
venv/bin/python scrape.py                    # update ../licenses.txt only
venv/bin/python scrape.py --commit           # ...and commit if it changed
venv/bin/python scrape.py --push             # ...and push (implies --commit)
venv/bin/python scrape.py --from-file page.html --dry-run   # test the parser offline
```

Progress goes to stderr, the one-line result to stdout. Exit status is 0 whether
or not anything changed, 1 on failure. Python 3.10 or newer.

## GitHub Actions

The workflow runs daily at 06:37 UTC (18:37 NZST / 19:37 NZDT) and on demand from
the repository's *Actions* tab (*Scrape licence holders* → *Run workflow*), or:

```bash
gh workflow run scrape.yml --repo Chill-Division/mca-licenses
```

Commits are authored by `github-actions[bot]` using the workflow's own token,
which the workflow file grants `contents: write`. On failure the fetched page is
kept as a run artifact for seven days so the cause can be inspected.

Things to know:

- **60-day rule.** GitHub disables scheduled workflows in repositories with no
  activity for 60 days. The scraper's own commits count as activity, and the
  Ministry updates the list every few weeks, so this should not trigger. If the
  schedule ever stops, re-enable it from the *Actions* tab.
- **Runner IPs.** GitHub runners use datacenter IP ranges. curl_cffi has been
  passing Cloudflare from them, but if the Ministry tightens its rules the runs
  will fail with `Cloudflare challenge page` in the log; the fallback is the
  cron setup below from a server with an ordinary IP.

## Fallback: cron on a server (e.g. Dreamhost) with a deploy key

```bash
# once, on the server
ssh-keygen -t ed25519 -f ~/.ssh/mca-licenses-deploy -N "" -C "mca-licenses deploy key"
cat ~/.ssh/mca-licenses-deploy.pub   # add at github.com/Chill-Division/mca-licenses/settings/keys, tick "Allow write access"
printf 'Host github.com-mca-licenses\n  HostName github.com\n  User git\n  IdentityFile ~/.ssh/mca-licenses-deploy\n  IdentitiesOnly yes\n' >> ~/.ssh/config
git clone git@github.com-mca-licenses:Chill-Division/mca-licenses.git ~/mca-licenses
cd ~/mca-licenses && git config user.name "mca-scraper" && git config user.email "mca-scraper@users.noreply.github.com"
python3 -m venv scraper/venv && scraper/venv/bin/pip install -r scraper/requirements.txt
```

Then in `crontab -e` (server local time):

```
37 18 * * * cd $HOME/mca-licenses && git pull -q --ff-only && scraper/venv/bin/python scraper/scrape.py --push >> $HOME/mca-scraper.log 2>&1
```

Dreamhost's shared hosting ships Python 3 and allows venvs; `curl_cffi` installs
from a pre-built wheel so no compiler is needed.

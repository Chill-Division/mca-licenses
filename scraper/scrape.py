#!/usr/bin/env python3
"""Keep licenses.txt in step with the Medicinal Cannabis Agency's licence holder page.

What it does
  1. Fetch the page. It sits behind a Cloudflare "managed challenge" that turns
     away ordinary HTTP clients (curl, wget, python-requests) on their TLS
     fingerprint alone, so the request is made with curl_cffi impersonating a
     real browser. Nothing else about the request is special.
  2. Parse the "Name of Licensee / Expiry date" table and the page's
     "Last updated" date.
  3. If the table differs from licenses.txt, rewrite the file (tab separated,
     page order, whitespace normalised). With --commit the change is committed
     using the page's Last-updated date as the subject (YYYYMMDD, the repo's
     long-standing convention) and a body listing what was added, removed or
     renewed. With --push it is pushed as well.

A change to the page's date with no change to the table is deliberately
ignored: the repository is a history of the licence list, not of the page.

Anything structurally surprising (no table, odd rows, suspiciously few rows,
a Cloudflare challenge page) aborts with exit status 1 before anything is
written, so a broken fetch can never be committed as a "change".
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from bs4 import BeautifulSoup
from curl_cffi import requests

URL = (
    "https://www.health.govt.nz/regulation-legislation/medicinal-cannabis/"
    "information-for-industry/current-licence-holders"
)
OUTPUT_NAME = "licenses.txt"
MIN_ROWS = 10  # the list has had 40+ entries for years; fewer means a broken page
FETCH_ATTEMPTS = 3
# Rotate browser fingerprints between attempts in case one of them gets flagged.
IMPERSONATE = ("chrome", "firefox", "safari")
NZ_OFFSET = dt.timezone(dt.timedelta(hours=12))  # fallback only; the page carries its own offset

Row = tuple[str, str]  # (licensee, expiry date as printed on the page)


class ScrapeError(Exception):
    """Anything that should stop the run without touching the repository."""


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


# ----------------------------------------------------------------------------- fetch
def looks_like_challenge(html: str) -> bool:
    head = html[:20_000]
    return "_cf_chl_opt" in head or "<title>Just a moment" in head


def fetch(url: str, timeout: float = 30.0) -> str:
    last_error = "no attempts made"
    for attempt in range(1, FETCH_ATTEMPTS + 1):
        profile = IMPERSONATE[(attempt - 1) % len(IMPERSONATE)]
        try:
            response = requests.get(
                url,
                impersonate=profile,
                timeout=timeout,
                headers={"Accept-Language": "en-NZ,en;q=0.9"},
            )
        except Exception as exc:  # network, TLS, timeout
            last_error = f"{type(exc).__name__}: {exc}"
        else:
            if response.status_code == 200 and not looks_like_challenge(response.text):
                log(f"fetched {len(response.text):,} chars (attempt {attempt}, {profile})")
                return response.text
            if looks_like_challenge(response.text):
                last_error = f"Cloudflare challenge page (impersonating {profile})"
            else:
                last_error = f"HTTP {response.status_code} (impersonating {profile})"
        log(f"attempt {attempt}/{FETCH_ATTEMPTS} failed: {last_error}")
        if attempt < FETCH_ATTEMPTS:
            time.sleep(5 * attempt)
    raise ScrapeError(f"could not fetch {url}: {last_error}")


# ----------------------------------------------------------------------------- parse
def clean(text: str) -> str:
    """Collapse runs of whitespace (including non-breaking spaces) to one space."""
    return re.sub(r"\s+", " ", text).strip()


def find_table(soup: BeautifulSoup):
    seen = []
    for table in soup.find_all("table"):
        header = [clean(th.get_text(" ")).lower() for th in table.find_all("th")]
        seen.append(header)
        if len(header) >= 2 and "licensee" in header[0] and "expir" in header[1]:
            return table
    raise ScrapeError(f"licence table not found; table headers seen: {seen!r}")


def parse_nz_date(text: str) -> dt.date | None:
    try:
        return dt.datetime.strptime(text, "%d %B %Y").date()
    except ValueError:
        return None


def parse_rows(table) -> list[Row]:
    rows: list[Row] = []
    for tr in table.find_all("tr"):
        cells = tr.find_all(["th", "td"])
        if not cells or all(cell.name == "th" for cell in cells):
            continue  # header row
        values = [clean(cell.get_text(" ")) for cell in cells]
        if not any(values):
            continue  # empty spacer row
        if len(values) != 2:
            raise ScrapeError(f"expected 2 cells per row, got {len(values)}: {values!r}")
        name, expiry = values
        if not name:
            log(f"warning: skipping row with empty licensee name: {values!r}")
            continue
        if not expiry:
            log(f"warning: no expiry date for {name!r}")
        elif parse_nz_date(expiry) is None:
            log(f"warning: unrecognised expiry date {expiry!r} for {name!r}")
        rows.append((name, expiry))
    if len(rows) < MIN_ROWS:
        raise ScrapeError(
            f"only {len(rows)} rows parsed (minimum {MIN_ROWS}); refusing to continue"
        )
    return rows


def parse_last_updated(soup: BeautifulSoup) -> dt.datetime | None:
    """The page footer reads: Last updated <time datetime="...">11 September 2026</time>."""
    node = soup.select_one(".block--moh-last-changed-date time[datetime]")
    if node is None:
        marker = soup.find(string=re.compile(r"last updated", re.I))
        node = marker.find_next("time") if marker else None
    if node is not None and node.get("datetime"):
        try:
            return dt.datetime.fromisoformat(node["datetime"])
        except ValueError:
            pass
    match = re.search(r"Last updated\s+(\d{1,2} \w+ \d{4})", soup.get_text(" "), re.I)
    if match:
        parsed = parse_nz_date(match.group(1))
        if parsed:
            return dt.datetime.combine(parsed, dt.time(), tzinfo=NZ_OFFSET)
    return None


# ----------------------------------------------------------------------------- compare
def serialise(rows: list[Row]) -> str:
    return "".join(f"{name}\t{expiry}\n" for name, expiry in rows)


def load_existing(path: Path) -> list[Row]:
    if not path.exists():
        return []
    rows: list[Row] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            name, _, expiry = line.partition("\t")
            rows.append((name, expiry))
    return rows


def describe_change(old: list[Row], new: list[Row]) -> list[str]:
    before, after = dict(old), dict(new)
    lines = [f"+ {n} ({after[n]})" for n in after if n not in before]
    lines += [f"- {n} ({before[n]})" for n in before if n not in after]
    lines += [
        f"~ {n}: {before[n]} -> {after[n]}"
        for n in after
        if n in before and before[n] != after[n]
    ]
    return lines or ["(row order or formatting changed only)"]


# ----------------------------------------------------------------------------- git
def git(root: Path, *args: str, capture: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, text=True, capture_output=capture
    )


def commit(root: Path, rel_path: str, subject: str, body: str, push: bool) -> None:
    git(root, "add", "--", rel_path)
    staged = subprocess.run(
        ["git", "-C", str(root), "diff", "--cached", "--quiet", "--", rel_path]
    )
    if staged.returncode == 0:
        log("nothing staged; skipping commit")
        return
    # Passing the path limits the commit to this file even if other changes are staged.
    git(root, "commit", "--quiet", "-m", subject, "-m", body, "--", rel_path)
    sha = git(root, "rev-parse", "--short", "HEAD", capture=True).stdout.strip()
    log(f"committed {sha}: {subject}")
    if push:
        git(root, "push", "--quiet")
        log("pushed")


# ----------------------------------------------------------------------------- GitHub Actions glue
def github_outputs(**values) -> None:
    """Expose results as step outputs when running inside GitHub Actions."""
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            for key, value in values.items():
                fh.write(f"{key}={value}\n")


def step_summary(markdown: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(markdown + "\n")


# ----------------------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(
        description="Update licenses.txt from the MCA current licence holders page."
    )
    parser.add_argument(
        "--repo-root", type=Path, default=here.parent,
        help="repository containing licenses.txt (default: parent of this script)",
    )
    parser.add_argument("--url", default=URL, help="page to scrape (default: the MCA page)")
    parser.add_argument(
        "--from-file", type=Path, metavar="HTML",
        help="parse a saved copy of the page instead of fetching (for testing)",
    )
    parser.add_argument(
        "--save-html", type=Path, metavar="PATH",
        help="write the fetched page here (kept as a debugging artifact in CI)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="report what would change but do not write, commit or push",
    )
    parser.add_argument("--commit", action="store_true", help="git commit licenses.txt if it changed")
    parser.add_argument("--push", action="store_true", help="git push after committing (implies --commit)")
    args = parser.parse_args(argv)
    if args.push:
        args.commit = True

    try:
        if args.from_file:
            log(f"parsing saved page {args.from_file}")
            html = args.from_file.read_text(encoding="utf-8")
        else:
            html = fetch(args.url)
        if args.save_html:
            args.save_html.write_text(html, encoding="utf-8")
        soup = BeautifulSoup(html, "html.parser")
        rows = parse_rows(find_table(soup))
        updated = parse_last_updated(soup)
    except ScrapeError as exc:
        log(f"error: {exc}")
        return 1

    note = ""
    if updated is None:
        log("warning: 'Last updated' not found on the page; using today's NZ date instead")
        updated = dt.datetime.now(NZ_OFFSET)
        note = " (page date not found; scrape date used)"
    stamp = updated.strftime("%Y%m%d")

    output = args.repo_root / OUTPUT_NAME
    old_text = output.read_text(encoding="utf-8") if output.exists() else ""
    new_text = serialise(rows)
    changed = new_text != old_text

    summary = (
        f"{len(rows)} licence holders; page last updated {updated:%Y-%m-%d}{note}; "
        f"{OUTPUT_NAME} {'CHANGED' if changed else 'unchanged'}"
    )
    print(summary)
    github_outputs(changed=str(changed).lower(), last_updated=stamp, rows=len(rows))
    if not changed:
        step_summary(summary)
        return 0

    details = describe_change(load_existing(output), rows)
    for line in details:
        print(f"  {line}")
    step_summary(f"**{summary}**\n\n```\n" + "\n".join(details) + "\n```")

    if args.dry_run:
        log("dry run: nothing written")
        return 0

    output.write_text(new_text, encoding="utf-8")
    log(f"wrote {output}")

    if args.commit:
        body = (
            f"Source: {args.url}\n"
            f"Page last updated: {updated.day} {updated:%B %Y}{note}\n\n" + "\n".join(details)
        )
        try:
            commit(args.repo_root, OUTPUT_NAME, stamp, body, push=args.push)
        except subprocess.CalledProcessError as exc:
            log(f"error: git command failed: {exc}")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

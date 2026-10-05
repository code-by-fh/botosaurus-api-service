"""Regenerate app/browser/data/tracker_domains.txt from EasyPrivacy, EasyList and Tranco.

Readiness ignores requests to the listed domains and their subdomains: they never hold a
render back (``network_activity.InflightTracker``, ADR 0005). A domain that wrongly lands
on the list can let a render return before content it delivers has arrived, so the
selection errs towards leaving domains off.

Run manually by a maintainer from the repository root
(``python -m scripts.update_tracker_domains``); the service
itself never downloads anything, so its startup stays deterministic and its egress untouched.

Selection:
1. Domain-wide blocking rules (``||domain^``) from the sections that list tracking and ad
   servers (EasyPrivacy ``trackingservers*``, EasyList ``adservers``) whose options are only
   ``third-party`` or resource types. Other sections also block real sites as embeds
   (``||soundcloud.com^$ping``); rules scoped to paths or to sites say nothing about the
   whole domain.
2. Minus every domain that either list exempts as a whole (``@@||domain^``): the list
   maintainers found that blocking it breaks pages.
3. Minus the anti-bot vendors the service never blocks (``NEVER_BLOCKED_DOMAINS``) and
   NEVER_LISTED_DOMAINS (tag and consent managers, experimentation, fraud detection).
4. Minus subdomains of a domain already listed; readiness ignores subdomains anyway.
5. The MAX_DOMAINS best-ranked of the rest by the Tranco top-1M list (a host takes the rank
   of its best-ranked parent domain), so that the list is bounded and holds the domains
   pages actually load. Unranked domains are dropped.
"""

import csv
import io
import re
import sys
import urllib.request
import zipfile
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from app.browser.blocking import DOMAIN_SYNTAX, NEVER_BLOCKED_DOMAINS

FILTER_LIST_URLS = (
    "https://easylist.to/easylist/easyprivacy.txt",
    "https://easylist.to/easylist/easylist.txt",
)
RANKING_URL = "https://tranco-list.eu/top-1m.csv.zip"
OUTPUT_PATH = Path(__file__).resolve().parent.parent / "app/browser/data/tracker_domains.txt"
# Nothing is blocked; readiness only ignores requests to listed domains. Every entry risks
# ignoring a request that delivers content, so only the domains pages load most are kept.
MAX_DOMAINS = 1000
DOWNLOAD_TIMEOUT_SECONDS = 300
# easylist.to rejects urllib's default user agent with 403.
DOWNLOAD_USER_AGENT = "page-render-service-tracker-list-updater/1"
SECTION_MARKER = "! *** easylist:"
SERVER_SECTIONS = re.compile(
    r"^easyprivacy/easyprivacy_trackingservers[a-z_]*\.txt$|^easylist/easylist_adservers\.txt$"
)
SHARED_HOSTING_DOMAINS = frozenset(
    {
        "amazonaws.com",
        "cloudfront.net",
        "windows.net",
        "appspot.com",
        "googleapis.com",
        "pages.dev",
        "workers.dev",
        "vercel.app",
        "netlify.app",
        "github.io",
        "herokuapp.com",
        "firebaseapp.com",
        "web.app",
        "azureedge.net",
        "akamaihd.net",
        "ahacdn.me",
        "ahcdn.com",
        "netlify.com",
        "now.sh",
        "r2.dev",
    }
)
DOMAIN_RULE = re.compile(r"^\|\|([a-z0-9.-]+\.[a-z]{2,})\^(?:\$(.+))?$")
EXEMPTION_RULE = re.compile(r"^@@\|\|([a-z0-9.-]+\.[a-z]{2,})\^(?:\$.*)?$")
# Options that narrow a rule by request type only; the domain stays a tracker.
DOMAIN_WIDE_OPTIONS = frozenset(
    {
        "third-party",
        "3p",
        "script",
        "image",
        "xmlhttprequest",
        "subdocument",
        "media",
        "font",
        "stylesheet",
        "object",
        "other",
    }
)
# Never listed, even if a filter list blocks them, in addition to NEVER_BLOCKED_DOMAINS,
# because readiness must keep waiting for them. Fraud-detection probes and Turnstile
# (cloudflare.com outside challenges.cloudflare.com) gate access to the page.
# Experimentation, personalisation and geolocation services decide which content a page
# shows (and anti-flicker snippets hide the page until they answer); video players may
# not start before the IMA ad SDK answered. Tag and consent managers: sites load content,
# A/B-test variants or the consent state that unlocks content through them, and
# EasyPrivacy carries hundreds of site exceptions for Google Tag Manager for that reason.
NEVER_LISTED_DOMAINS = frozenset(
    {
        "googletagmanager.com",
        "tagmanager.google.com",
        "tealiumiq.com",
        "tiqcdn.com",
        "ensighten.com",
        "cookielaw.org",
        "onetrust.com",
        "cookiebot.com",
        "usercentrics.eu",
        "didomi.io",
        "trustarc.com",
        "consensu.org",
        "privacy-mgmt.com",
        "sourcepoint.com",
        "quantcast.mgr.consensu.org",
        "cloudflare.com",
        "statsig.com",
        "statsigapi.net",
        "launchdarkly.com",
        "optimizely.com",
        "visualwebsiteoptimizer.com",
        "abtasty.com",
        "kameleoon.eu",
        "split.io",
        "dynamicyield.com",
        "clearbit.com",
        "db-ip.com",
        "imasdk.googleapis.com",
        "forter.com",
        "riskified.com",
        "sift.com",
        "siftscience.com",
        "fpjs.io",
        "fpcdn.io",
        "fingerprint.com",
        "online-metrix.net",
        "clearsale.com.br",
    }
)


def domain_wide_rules(lines: Iterable[str]) -> set[str]:
    """Domains that a server section blocks as a whole, apart from request-type restrictions."""
    domains = set()
    in_server_section = False
    for line in (raw.strip() for raw in lines):
        if line.startswith(SECTION_MARKER):
            in_server_section = _is_server_section(line)
            continue
        match = DOMAIN_RULE.match(line) if in_server_section else None
        if match and _only_domain_wide_options(match.group(2)) and _is_domain(match.group(1)):
            domains.add(match.group(1))
    return domains


def _is_domain(candidate: str) -> bool:
    # The service refuses to start on a line its loader cannot parse.
    return DOMAIN_SYNTAX.match(candidate) is not None


def _is_server_section(marker: str) -> bool:
    name = marker.removeprefix(SECTION_MARKER).removesuffix("***").strip()
    return SERVER_SECTIONS.match(name) is not None


def _only_domain_wide_options(options: str | None) -> bool:
    return options is None or set(options.split(",")) <= DOMAIN_WIDE_OPTIONS


def exempted_domains(lines: Iterable[str]) -> set[str]:
    """Domains that a filter list exempts as a whole on at least some sites."""
    return {match.group(1) for line in lines if (match := EXEMPTION_RULE.match(line.strip()))}


def is_covered(domain: str, domains: frozenset[str] | set[str]) -> bool:
    """Whether ``domain`` or one of its parent domains is in ``domains``."""
    labels = domain.split(".")
    return any(".".join(labels[index:]) in domains for index in range(len(labels) - 1))


def select_domains(candidates: set[str], excluded: set[str], ranking: dict[str, int]) -> list[str]:
    """Apply steps 2 to 5 of the module docstring; the result is sorted alphabetically."""
    kept = {domain for domain in candidates if not is_covered(domain, excluded)}
    roots = {domain for domain in kept if not _has_listed_parent(domain, kept)}
    ranks = {domain: rank for domain in roots if (rank := _rank(domain, ranking)) is not None}
    return sorted(sorted(ranks, key=ranks.__getitem__)[:MAX_DOMAINS])


def _rank(domain: str, ranking: dict[str, int]) -> int | None:
    # Tranco ranks registrable domains; a tracking host such as g.doubleclick.net takes the
    # rank of the best-ranked domain it belongs to. A customer's host on a shared hosting
    # platform says nothing about how often pages load it, so it inherits no rank.
    labels = domain.split(".")
    parents = [".".join(labels[index:]) for index in range(len(labels) - 1)]
    if any(parent in SHARED_HOSTING_DOMAINS for parent in parents[1:]):
        parents = parents[:1]
    return min((ranking[parent] for parent in parents if parent in ranking), default=None)


def _has_listed_parent(domain: str, domains: set[str]) -> bool:
    parent = domain.partition(".")[2]
    return bool(parent) and is_covered(parent, domains)


def _download(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": DOWNLOAD_USER_AGENT})
    with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
        return response.read()


def _ranking(archive: bytes) -> dict[str, int]:
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        text = bundle.read(bundle.namelist()[0]).decode()
    return {domain: int(rank) for rank, domain in csv.reader(io.StringIO(text))}


def _header(count: int) -> str:
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        f"# Generated by scripts/update_tracker_domains.py at {stamp}; do not edit by hand.\n"
        f"# {count} domains from EasyPrivacy and EasyList (https://easylist.to, licensed\n"
        "# GPLv3 or CC BY-SA 3.0), ranked by the Tranco list (https://tranco-list.eu).\n"
    )


def main() -> int:
    """Download the sources and rewrite OUTPUT_PATH."""
    lines = [line for url in FILTER_LIST_URLS for line in _download(url).decode().splitlines()]
    excluded = exempted_domains(lines) | NEVER_BLOCKED_DOMAINS | NEVER_LISTED_DOMAINS
    domains = select_domains(domain_wide_rules(lines), excluded, _ranking(_download(RANKING_URL)))
    OUTPUT_PATH.write_text(_header(len(domains)) + "\n".join(domains) + "\n", encoding="utf-8")
    print(f"Wrote {len(domains)} domains to {OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

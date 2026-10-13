"""Shared bits for scripts that call Bulbapedia's MediaWiki API.

Bulbapedia sits behind Cloudflare. A request with Python's default
User-Agent (or a browser-looking one) gets a 403 bot challenge
("Just a moment...", header `cf-mitigated: challenge`) from any IP. The
descriptive USER_AGENT below gets 200 from a home connection (checked
2026-10-05). From GitHub-hosted runners the same User-Agent worked in the
weekly ptcg-refresh through 2026-09-07 and has been refused with 403 in
every run since 2026-09-14: Cloudflare now challenges the runners'
datacenter IPs whatever the User-Agent says. So in CI a 403 here means
"run this from a local machine", and the scripts say so instead of a
bare "HTTP 403 Forbidden".
"""

from __future__ import annotations

import os
import urllib.error

BULBAPEDIA_API = "https://bulbapedia.bulbagarden.net/w/api.php"
USER_AGENT = "OPBindr-Bot/1.0 (contact: arjun@neuroplexlabs.com)"


def describe_http_error(e: urllib.error.HTTPError, script: str) -> str:
    """One message for a failed Bulbapedia call, naming the likely cause."""
    msg = f"HTTP {e.code} from Bulbapedia: {e.reason}"
    challenged = (e.headers.get("cf-mitigated") or "").lower() == "challenge"
    if e.code == 403 and (challenged or os.environ.get("GITHUB_ACTIONS")):
        where = ("this GitHub-hosted runner's IP"
                 if os.environ.get("GITHUB_ACTIONS") else "this IP")
        msg += (
            f"\n   Cloudflare {'bot challenge' if challenged else 'block'} on "
            f"{where}, not a User-Agent problem (the same request gets 200 "
            f"from a home connection).\n"
            f"   Run it locally instead: python -m scripts.{script}"
        )
    return msg

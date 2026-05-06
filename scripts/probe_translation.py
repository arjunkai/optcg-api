"""
Smoke-probe for the eBay Commerce Translation API.

The Translation API is open to any registered dev (no Application
Growth Check). This script tests JA→EN translation of a few card
names so you can see real output before wiring it into the JA pricing
backfill.

Use case: when querying EBAY_US for JA-card prices, US sellers list
in English. The current pipeline relies on hand-curated query strings.
Auto-translating the JA card name (`name_ja`) to a search-friendly EN
string would let the JA backfill run without per-card curation.

Run:
    python -m scripts.probe_translation "リザードンex" "ピカチュウ" "ミュウツーGX"

Required env: EBAY_APP_ID, EBAY_CERT_ID.
"""

from __future__ import annotations

import argparse
import sys

from scripts.ebay_client import EbayAccessDeniedError, EbayClient


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("texts", nargs="+", help="Strings to translate")
    ap.add_argument("--from", dest="from_lang", default="ja")
    ap.add_argument("--to", dest="to_lang", default="en")
    ap.add_argument("--context", default="ITEM_TITLE", choices=["ITEM_TITLE", "ITEM_DESCRIPTION"])
    args = ap.parse_args()

    client = EbayClient()
    print(f"Translating {len(args.texts)} string(s) {args.from_lang}→{args.to_lang} (context={args.context})...")
    try:
        translations = client.translate(
            args.texts, from_lang=args.from_lang, to_lang=args.to_lang, context=args.context
        )
    except EbayAccessDeniedError as e:
        print("ACCESS DENIED:", e, file=sys.stderr)
        return 2
    except Exception as e:
        msg = str(e)
        print("FAILED:", msg, file=sys.stderr)
        if "invalid_scope" in msg or "exceeds the scope" in msg:
            print(
                "\nThe commerce.translation scope is not enabled on this keyset. "
                "To enable: open https://developer.ebay.com/my/keys, click OAuth Scopes "
                "next to your production keyset, find 'Commerce Translation API' in the "
                "list and tick its checkbox, save. Then delete data/.ebay_tokens/ and "
                "re-run this probe.",
                file=sys.stderr,
            )
        return 1

    print()
    for src, dst in zip(args.texts, translations):
        print(f"  {src}  →  {dst}")

    return 0


if __name__ == "__main__":
    sys.exit(main())

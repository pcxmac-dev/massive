"""Fetch the previous day's OHLC bar for a ticker from the Massive API.

The API key is read from the MASSIVE_API_KEY environment variable:
  - locally:        set it in your shell / user environment
  - GitHub Actions: stored as a repository secret, injected by the workflow
"""
import json
import os
import sys
import urllib.error
import urllib.request

BASE_URL = "https://api.massive.com"


def main() -> int:
    api_key = os.environ.get("MASSIVE_API_KEY")
    if not api_key:
        print("MASSIVE_API_KEY is not set", file=sys.stderr)
        return 1

    ticker = sys.argv[1] if len(sys.argv) > 1 else "AAPL"
    url = f"{BASE_URL}/v2/aggs/ticker/{ticker}/prev"
    # Send the key in a header rather than the URL so it never appears in logs.
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}: {e.read().decode(errors='replace')}", file=sys.stderr)
        return 1

    for bar in data.get("results", []):
        print(f"{ticker}: open={bar['o']} high={bar['h']} low={bar['l']} close={bar['c']} volume={bar['v']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/bin/bash
# Double-click this file in Finder to start Scraper Buddy.
#
# Starting it this way (or typing `scraper` in Terminal) matters on a Mac:
# macOS traces the processes started by a *pasted* Terminal command and stops
# them the moment one reaches a website on Apple's unsafe list - a scrape
# visits tens of thousands of business websites, so a pasted start eventually
# ends with "Malicious Script Blocked". A double-click or a typed command is
# not a paste. See README.md, "If something breaks mid-run".
cd "$(dirname "$0")" || exit 1
if [ ! -x .venv/bin/scraper ]; then
  echo "Setting up (first time only)..."
  python3 -m venv .venv && .venv/bin/pip install -q -e . || {
    echo "Setup failed. Open Terminal here and run: make dev"
    read -r -p "Press return to close this window."
    exit 1
  }
fi
.venv/bin/scraper "$@"
echo
read -r -p "Press return to close this window."

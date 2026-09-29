#!/bin/bash
# Starts Chrome with a timezone and language that match the egress IP.
#
# Chrome on Linux ignores --lang and takes its language from LANGUAGE, and the
# timezone JavaScript sees (including inside Web Workers) from TZ. Setting both
# only here keeps the service process itself in UTC.
set -euo pipefail

locale="${BROWSER_LOCALE:-de-DE}"
export LANGUAGE="${locale//-/_}"
export TZ="${BROWSER_TIMEZONE:-Europe/Berlin}"

exec /usr/bin/google-chrome "$@"

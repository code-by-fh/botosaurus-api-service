#!/bin/bash
# Starts Chrome with a timezone and language that match the egress IP, and
# without the service's secrets in its environment.
#
# Chrome on Linux ignores --lang and takes its language from LANGUAGE, and the
# timezone JavaScript sees (including inside Web Workers) from TZ. Setting both
# only here keeps the service process itself in UTC.
set -euo pipefail

# Chrome inherits the service environment. Pages never read it, but a compromised
# renderer, a crash dump or /proc/<pid>/environ could; Chrome needs none of these.
SECRET_VARIABLES=(API_KEYS API_KEY HOME_PROXY PROXY VNC_PASSWORD)
unset "${SECRET_VARIABLES[@]}"

locale="${BROWSER_LOCALE:-de-DE}"
export LANGUAGE="${locale//-/_}"
export TZ="${BROWSER_TIMEZONE:-Europe/Berlin}"

exec /usr/bin/google-chrome "$@"

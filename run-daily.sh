#!/bin/zsh
# Daily outreach run. Started by launchd (Mon-Fri 10:00) or by hand.
# Shows a macOS notification when it starts, finishes, or fails.

# launchd starts in "/" with a tiny PATH, so set both here.
cd "${0:A:h}" || exit 1
export PATH="$HOME/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
setopt pipefail

# A missing notification must never stop the job, so errors are ignored.
notify() {
  osascript -e 'on run argv
    display notification (item 2 of argv) with title (item 1 of argv)
  end run' "$1" "$2" >/dev/null 2>&1 || true
}

fail() {
  echo "FAILED: $1"
  notify "Outreach failed" "$1"
  exit 1
}

# Run one step. Stop the whole job if it fails.
run_step() {
  local name=$1; shift
  echo "--- $name"
  "$@"
  local rc=$?
  (( rc == 0 )) || fail "$name failed (exit $rc). See ~/Library/Logs/outreach-daily.log"
}

echo "=== $(date '+%F %T') outreach daily: start ==="
notify "Outreach started" "Daily outreach job has started."

[[ -f .env ]] || fail ".env file not found"
source .env
[[ -n $GMAIL_ADDRESS && -n $GMAIL_APP_PASSWORD ]] \
  || fail "GMAIL_ADDRESS or GMAIL_APP_PASSWORD is missing in .env"

run_step "emailmap predict" python3 emailmap.py predict
run_step "emailmap predict --apply" python3 emailmap.py predict --apply

# Keep a copy of the send output so the last line can go in the notification.
send_out=$(mktemp)
trap 'rm -f "$send_out"' EXIT
send() { python3 outreach.py send --ignore-window 2>&1 | tee "$send_out"; }
run_step "outreach send" send

summary=$(grep -v '^[[:space:]]*$' "$send_out" | tail -n 1 | cut -c1-200)
echo "=== $(date '+%F %T') outreach daily: done ==="
notify "Outreach completed" "${summary:-Finished with no output.}"

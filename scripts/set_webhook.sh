#!/usr/bin/env bash
# Add or replace the notification webhook without pasting it into a chat.
#
# Usage:  ./scripts/set_webhook.sh            (prompts, input hidden)
#         ./scripts/set_webhook.sh <url>
#
# Writes NOTIFY_WEBHOOK_URL into .env, recreates the container so it is
# picked up, and fires a test notification so you see it land.
set -euo pipefail

cd "$(dirname "$0")/.."
ENV_FILE=".env"

if [ ! -f "$ENV_FILE" ]; then
    echo "No .env here. Run this from the deployment directory." >&2
    exit 1
fi

URL="${1:-}"
if [ -z "$URL" ]; then
    # -s so the credential is not echoed to the terminal or scrollback.
    echo "This wants your Slack/Discord WEBHOOK URL - it starts with"
    echo "  https://hooks.slack.com/services/..."
    echo "It is NOT your app passphrase and NOT your SSH key."
    read -rsp "Paste the webhook URL (input hidden), then Enter: " URL
    echo
fi

case "$URL" in
    https://hooks.slack.com/services/*) LABEL="wanax" ;;
    https://discord.com/api/webhooks/*|https://discordapp.com/api/webhooks/*)
        LABEL="wanax" ;;
    https://*) echo "Warning: not a recognised Slack/Discord webhook host." ;;
    *)
        echo >&2
        echo "That does not look like an https:// URL, so nothing was changed." >&2
        echo "If you pasted a passphrase or key by mistake, rotate it to be safe." >&2
        exit 1 ;;
esac

# Replace any existing line rather than appending a duplicate: env_file
# takes the LAST occurrence, which makes stale duplicates confusing.
tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
grep -v -E '^(NOTIFY_WEBHOOK_URL|NOTIFY_LABEL)=' "$ENV_FILE" > "$tmp" || true
{
    echo "NOTIFY_WEBHOOK_URL=${URL}"
    echo "NOTIFY_LABEL=${LABEL:-wanax}"
} >> "$tmp"
cat "$tmp" > "$ENV_FILE"          # preserve mode 600 on the original
chmod 600 "$ENV_FILE"
echo "Wrote NOTIFY_WEBHOOK_URL to .env (mode 600)."

# env_file is read at container CREATE time, so a restart is not enough.
echo "Recreating the app container so it reads the new value..."
docker compose up -d --force-recreate web >/dev/null
sleep 8

echo "Sending a test notification..."
docker compose exec -T web python3 -c "
import notify
if not notify.enabled():
    raise SystemExit('Notifications still disabled - the container did not pick up the value.')
ok = notify.notify('Notifications are live',
                   'Your Kalshi node can now reach you. Circuit-breaker '
                   'trips and the daily digest will arrive this way.',
                   level='info', key='setup-test', throttle=0)
print('Test notification sent.' if ok else 'Send FAILED - check the URL.')
"
echo "Done. Check your Slack channel."

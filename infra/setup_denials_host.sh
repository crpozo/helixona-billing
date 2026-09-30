#!/usr/bin/env bash
# One-time host setup for the Denials bot's screen and queue. Run ON the EC2
# host, as root or with sudo:
#
#     sudo bash /opt/helixona-agent/setup_denials_host.sh
#
# It gives the bot what the other three already have — a display (:103), a
# VNC server on it (5904) behind noVNC (6084), and its SQS queue with the URL
# in /opt/helixona-agent/.env — then restarts the bot and the dashboard.
# Idempotent: what already exists is left alone. Nothing here touches the
# other bots' displays or queues.
set -euo pipefail

DISPLAY_NO=${1:-103}
NOVNC_PORT=${2:-6084}
RFB_PORT=$((5900 + DISPLAY_NO - 99))          # :99→5900 … :103→5904, like the others
APP=/opt/helixona-agent
ENV_FILE=$APP/.env
QUEUE_NAME=helixona-agent-tasks-denials
VNCPASS=$APP/.vncpass
NOVNC_WEB=/usr/share/novnc
[ -d "$NOVNC_WEB" ] || NOVNC_WEB=$(dirname "$(find / -name vnc.html -path '*novnc*' 2>/dev/null | head -1)")

say() { printf '\n▶ %s\n' "$*"; }

# ── 1. The display ────────────────────────────────────────────────────────
say "Display :$DISPLAY_NO"
if [ ! -f /etc/systemd/system/xvfb@.service ]; then
    cat > /etc/systemd/system/xvfb@.service <<'UNIT'
[Unit]
Description=Virtual X display :%i
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/Xvfb :%i -screen 0 1280x1024x24
Restart=always

[Install]
WantedBy=multi-user.target
UNIT
    echo "  wrote xvfb@.service (template)"
fi

# ── 2. VNC on that display, noVNC in front of it ─────────────────────────
say "VNC $RFB_PORT · noVNC $NOVNC_PORT"
if [ ! -f "$VNCPASS" ]; then
    # The other bots' x11vnc read this file; if it is elsewhere, copy the
    # -rfbauth path from `ps aux | grep '[x]11vnc'` and re-run.
    echo "  ⚠️ $VNCPASS not found — the live screen will ask for no password until you run:"
    echo "     x11vnc -storepasswd YOUR_PASSWORD $VNCPASS"
    AUTH=""
else
    AUTH="-rfbauth $VNCPASS"
fi
cat > /etc/systemd/system/x11vnc-$DISPLAY_NO.service <<UNIT
[Unit]
Description=x11vnc on display :$DISPLAY_NO (Denials bot)
After=xvfb@$DISPLAY_NO.service
Requires=xvfb@$DISPLAY_NO.service

[Service]
Type=simple
ExecStart=/usr/bin/x11vnc -display :$DISPLAY_NO -rfbport $RFB_PORT -localhost -forever -shared $AUTH -noxdamage -quiet
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT
cat > /etc/systemd/system/novnc-$NOVNC_PORT.service <<UNIT
[Unit]
Description=noVNC on $NOVNC_PORT → VNC $RFB_PORT (Denials bot)
After=x11vnc-$DISPLAY_NO.service
Requires=x11vnc-$DISPLAY_NO.service

[Service]
Type=simple
ExecStart=$(command -v websockify || echo /usr/bin/websockify) --web=$NOVNC_WEB $NOVNC_PORT localhost:$RFB_PORT
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now xvfb@$DISPLAY_NO x11vnc-$DISPLAY_NO novnc-$NOVNC_PORT
sleep 1
systemctl is-active xvfb@$DISPLAY_NO x11vnc-$DISPLAY_NO novnc-$NOVNC_PORT | paste - - - | sed 's/^/  xvfb · x11vnc · novnc: /'

# ── 3. The queue ──────────────────────────────────────────────────────────
say "Queue $QUEUE_NAME"
if grep -q '^SQS_QUEUE_URL_DENIALS=.\+' "$ENV_FILE" 2>/dev/null; then
    echo "  already in .env: $(grep '^SQS_QUEUE_URL_DENIALS=' "$ENV_FILE")"
else
    REGION=$(grep -E '^AWS_REGION=' "$ENV_FILE" 2>/dev/null | cut -d= -f2 || true)
    REGION=${REGION:-us-west-2}
    URL=""
    if command -v aws >/dev/null; then
        URL=$(aws sqs create-queue --queue-name "$QUEUE_NAME" --region "$REGION" --query QueueUrl --output text 2>/dev/null || true)
    fi
    if [ -z "$URL" ] && [ -x "$APP/venv/bin/python" ]; then
        URL=$("$APP/venv/bin/python" - "$QUEUE_NAME" "$REGION" <<'PY' 2>/dev/null || true
import sys, boto3
print(boto3.client('sqs', region_name=sys.argv[2]).create_queue(QueueName=sys.argv[1])['QueueUrl'])
PY
)
    fi
    if [ -n "$URL" ]; then
        sed -i '/^SQS_QUEUE_URL_DENIALS=/d' "$ENV_FILE"
        echo "SQS_QUEUE_URL_DENIALS=$URL" >> "$ENV_FILE"
        echo "  created and written to .env: $URL"
    else
        echo "  ⚠️ could not create the queue from here (no AWS permission on the host)."
        echo "     Create it in the SQS console (same settings as helixona-agent-tasks-eob) and add to $ENV_FILE:"
        echo "     SQS_QUEUE_URL_DENIALS=https://sqs.$REGION.amazonaws.com/<account>/$QUEUE_NAME"
    fi
fi

# ── 4. Restart what reads the new pieces ─────────────────────────────────
say "Restart"
systemctl restart helixona-agent-denials helixona-dashboard
sleep 2
systemctl is-active helixona-agent-denials helixona-dashboard | paste - - | sed 's/^/  denials · dashboard: /'

cat <<MSG

Done. Two things only the AWS console can do:
  • Security group of 54.189.175.233: allow inbound TCP $NOVNC_PORT (the browser
    opens http://54.189.175.233:$NOVNC_PORT/vnc.html directly, like 6083).
  • If the queue could not be created above, create it there and add the URL to .env.
Then in the dashboard: 🚫 Denials → Live screen, and ▶ Run → Test on 5 claims.
MSG

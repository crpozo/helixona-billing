#!/bin/bash
# deploy_code.sh — Pushes the Billing Agent code to the EC2 instance
# Usage: ./deploy_code.sh <EC2_IP>

set -e

if [ -z "$1" ]; then
    echo "Usage: ./deploy_code.sh <EC2_STATIC_IP>"
    exit 1
fi

EC2_IP=$1
KEY_FILE="infra/helixona-agent-key.pem"
REMOTE_DIR="/opt/helixona-agent"

echo "Deploying Helixona Billing Agent to $EC2_IP..."

# Sync source code
rsync -avz -e "ssh -i $KEY_FILE -o StrictHostKeyChecking=no" \
    --exclude='venv' \
    --exclude='.env' \
    --exclude='__pycache__' \
    --exclude='infra/' \
    --exclude='setup_*.py' \
    ./ ubuntu@$EC2_IP:$REMOTE_DIR/

# Copy .env securely
scp -i $KEY_FILE -o StrictHostKeyChecking=no \
    .env ubuntu@$EC2_IP:$REMOTE_DIR/.env

# Copy the IV corrections systemd unit (Bot 2)
scp -i $KEY_FILE -o StrictHostKeyChecking=no \
    infra/helixona-agent-iv.service ubuntu@$EC2_IP:/tmp/helixona-agent-iv.service

# Install deps and restart the agent services
ssh -i $KEY_FILE -o StrictHostKeyChecking=no ubuntu@$EC2_IP << 'ENDSSH'
cd /opt/helixona-agent
source venv/bin/activate
pip install -r requirements.txt --quiet
sudo mv /tmp/helixona-agent-iv.service /etc/systemd/system/helixona-agent-iv.service
sudo systemctl daemon-reload
sudo systemctl enable helixona-agent-iv
sudo systemctl restart helixona-agent
sudo systemctl restart helixona-agent-iv
sudo systemctl restart helixona-dashboard
sleep 2
sudo systemctl status helixona-agent --no-pager
sudo systemctl status helixona-agent-iv --no-pager
sudo systemctl status helixona-dashboard --no-pager
echo "Deploy complete."
ENDSSH

echo "✅ Agent deployed and running on $EC2_IP"

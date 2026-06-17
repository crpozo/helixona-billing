"""
Provisions an EC2 instance in AWS to run the Helixona Billing Agent.
This replaces the "local clinic computer" — the Playwright browser
automation runs entirely in the cloud on this instance.

Creates:
  - Security Group (SSH + outbound internet)
  - Key Pair (for SSH access)
  - EC2 Instance (Ubuntu 22.04, t3.large)
  - Elastic IP (static IP so portals see a consistent address)
  - IAM Instance Profile (so the EC2 can talk to S3, SQS, DynamoDB, Bedrock)
"""
import boto3
import json
import time
import os
from dotenv import load_dotenv

load_dotenv()

REGION = os.environ.get('AWS_REGION', 'us-west-2')
INSTANCE_TYPE = 't3.large'  # 2 vCPU, 8GB RAM — runs 2 bots + headless Chromium
KEY_NAME = 'helixona-agent-key'
SG_NAME = 'helixona-agent-sg'
ROLE_NAME = 'helixona-agent-role'
PROFILE_NAME = 'helixona-agent-profile'

session = boto3.Session(
    aws_access_key_id=os.environ['AWS_ACCESS_KEY_ID'],
    aws_secret_access_key=os.environ['AWS_SECRET_ACCESS_KEY'],
    region_name=REGION
)

ec2 = session.client('ec2')
iam = session.client('iam')

# ──────────────────────────────────────────────
# 1. CREATE IAM ROLE + INSTANCE PROFILE
# ──────────────────────────────────────────────
trust_policy = json.dumps({
    "Version": "2012-10-17",
    "Statement": [{
        "Effect": "Allow",
        "Principal": {"Service": "ec2.amazonaws.com"},
        "Action": "sts:AssumeRole"
    }]
})

try:
    iam.create_role(
        RoleName=ROLE_NAME,
        AssumeRolePolicyDocument=trust_policy,
        Description='Helixona Billing Agent EC2 Role'
    )
    print(f"ROLE_CREATED:{ROLE_NAME}")
except iam.exceptions.EntityAlreadyExistsException:
    print(f"ROLE_EXISTS:{ROLE_NAME}")

# Attach permissions: S3, SQS, DynamoDB, Bedrock, Secrets Manager, CloudWatch
managed_policies = [
    'arn:aws:iam::aws:policy/AmazonS3FullAccess',
    'arn:aws:iam::aws:policy/AmazonSQSFullAccess',
    'arn:aws:iam::aws:policy/AmazonDynamoDBFullAccess',
    'arn:aws:iam::aws:policy/SecretsManagerReadWrite',
    'arn:aws:iam::aws:policy/CloudWatchFullAccessV2',
]

for policy_arn in managed_policies:
    try:
        iam.attach_role_policy(RoleName=ROLE_NAME, PolicyArn=policy_arn)
    except Exception:
        pass

# Bedrock access (custom inline policy)
bedrock_policy = json.dumps({
    "Version": "2012-10-17",
    "Statement": [{
        "Effect": "Allow",
        "Action": ["bedrock:*"],
        "Resource": "*"
    }]
})
try:
    iam.put_role_policy(
        RoleName=ROLE_NAME,
        PolicyName='BedrockFullAccess',
        PolicyDocument=bedrock_policy
    )
except Exception:
    pass

# Create Instance Profile and attach role
try:
    iam.create_instance_profile(InstanceProfileName=PROFILE_NAME)
    print(f"PROFILE_CREATED:{PROFILE_NAME}")
except iam.exceptions.EntityAlreadyExistsException:
    print(f"PROFILE_EXISTS:{PROFILE_NAME}")

try:
    iam.add_role_to_instance_profile(
        InstanceProfileName=PROFILE_NAME,
        RoleName=ROLE_NAME
    )
except iam.exceptions.LimitExceededException:
    pass  # Role already attached

# IAM propagation delay
print("Waiting 10s for IAM propagation...")
time.sleep(10)

# ──────────────────────────────────────────────
# 2. CREATE KEY PAIR (for SSH)
# ──────────────────────────────────────────────
key_path = os.path.join(os.path.dirname(__file__), f'{KEY_NAME}.pem')
try:
    key_response = ec2.create_key_pair(KeyName=KEY_NAME, KeyType='rsa')
    with open(key_path, 'w') as f:
        f.write(key_response['KeyMaterial'])
    os.chmod(key_path, 0o400)
    print(f"KEY_CREATED:{KEY_NAME} -> {key_path}")
except ec2.exceptions.ClientError as e:
    if 'InvalidKeyPair.Duplicate' in str(e):
        print(f"KEY_EXISTS:{KEY_NAME}")
    else:
        raise

# ──────────────────────────────────────────────
# 3. CREATE SECURITY GROUP
# ──────────────────────────────────────────────
try:
    # Get default VPC
    vpcs = ec2.describe_vpcs(Filters=[{'Name': 'isDefault', 'Values': ['true']}])
    vpc_id = vpcs['Vpcs'][0]['VpcId']

    sg_response = ec2.create_security_group(
        GroupName=SG_NAME,
        Description='Helixona Billing Agent - SSH + Outbound',
        VpcId=vpc_id
    )
    sg_id = sg_response['GroupId']

    # Allow SSH from anywhere (restrict to your IP in production)
    ec2.authorize_security_group_ingress(
        GroupId=sg_id,
        IpPermissions=[{
            'IpProtocol': 'tcp',
            'FromPort': 22,
            'ToPort': 22,
            'IpRanges': [{'CidrIp': '0.0.0.0/0', 'Description': 'SSH access'}]
        }]
    )
    print(f"SG_CREATED:{sg_id}")
except ec2.exceptions.ClientError as e:
    if 'InvalidGroup.Duplicate' in str(e):
        sgs = ec2.describe_security_groups(
            Filters=[{'Name': 'group-name', 'Values': [SG_NAME]}]
        )
        sg_id = sgs['SecurityGroups'][0]['GroupId']
        print(f"SG_EXISTS:{sg_id}")
    else:
        raise

# ──────────────────────────────────────────────
# 4. FIND UBUNTU 22.04 AMI
# ──────────────────────────────────────────────
images = ec2.describe_images(
    Filters=[
        {'Name': 'name', 'Values': ['ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*']},
        {'Name': 'state', 'Values': ['available']},
        {'Name': 'architecture', 'Values': ['x86_64']},
    ],
    Owners=['099720109477']  # Canonical
)
ami_id = sorted(images['Images'], key=lambda x: x['CreationDate'], reverse=True)[0]['ImageId']
print(f"AMI_SELECTED:{ami_id}")

# ──────────────────────────────────────────────
# 5. USER DATA SCRIPT (auto-installs everything on boot)
# ──────────────────────────────────────────────
user_data = """#!/bin/bash
set -e

# System updates
apt-get update -y
apt-get upgrade -y

# Install Python 3.11, pip, Docker
apt-get install -y python3.11 python3.11-venv python3-pip docker.io git unzip

# Install Playwright system dependencies
apt-get install -y libatk1.0-0 libatk-bridge2.0-0 libcups2 libxkbcommon0 \\
    libxcomposite1 libxdamage1 libxrandr2 libgbm1 libpango-1.0-0 \\
    libcairo2 libasound2 libnspr4 libnss3 xvfb fonts-liberation

# Install AWS CLI v2
curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "/tmp/awscliv2.zip"
unzip /tmp/awscliv2.zip -d /tmp
/tmp/aws/install

# Create app directory
mkdir -p /opt/helixona-agent
cd /opt/helixona-agent

# Create virtual environment
python3.11 -m venv venv
source venv/bin/activate

# Install Python dependencies
pip install playwright boto3 pydantic pydantic-settings python-dotenv tenacity
playwright install chromium
playwright install-deps chromium

# Create systemd service for the agent
cat > /etc/systemd/system/helixona-agent.service << 'EOF'
[Unit]
Description=Helixona Billing Agent
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/helixona-agent
ExecStart=/opt/helixona-agent/venv/bin/python src/main.py
Restart=always
RestartSec=10
Environment=DISPLAY=:99

[Install]
WantedBy=multi-user.target
EOF

# Create Xvfb service (virtual display for headless Chromium)
cat > /etc/systemd/system/xvfb.service << 'EOF'
[Unit]
Description=Virtual Framebuffer X Server
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/Xvfb :99 -screen 0 1280x1024x24
Restart=always

[Install]
WantedBy=multi-user.target
EOF

systemctl enable xvfb
systemctl start xvfb

echo "HELIXONA_AGENT_SETUP_COMPLETE" > /opt/helixona-agent/setup.log
"""

# ──────────────────────────────────────────────
# 6. LAUNCH EC2 INSTANCE
# ──────────────────────────────────────────────
print("Launching EC2 instance...")
run_response = ec2.run_instances(
    ImageId=ami_id,
    InstanceType=INSTANCE_TYPE,
    KeyName=KEY_NAME,
    SecurityGroupIds=[sg_id],
    IamInstanceProfile={'Name': PROFILE_NAME},
    MinCount=1,
    MaxCount=1,
    UserData=user_data,
    TagSpecifications=[{
        'ResourceType': 'instance',
        'Tags': [
            {'Key': 'Name', 'Value': 'helixona-billing-agent'},
            {'Key': 'Project', 'Value': 'helixona-v1'},
        ]
    }],
    BlockDeviceMappings=[{
        'DeviceName': '/dev/sda1',
        'Ebs': {
            'VolumeSize': 30,  # 30 GB
            'VolumeType': 'gp3',
            'DeleteOnTermination': True
        }
    }]
)

instance_id = run_response['Instances'][0]['InstanceId']
print(f"INSTANCE_LAUNCHED:{instance_id}")

# Wait for instance to be running
print("Waiting for instance to enter 'running' state...")
waiter = ec2.get_waiter('instance_running')
waiter.wait(InstanceIds=[instance_id])
print("Instance is running.")

# ──────────────────────────────────────────────
# 7. ALLOCATE + ASSOCIATE ELASTIC IP
# ──────────────────────────────────────────────
eip = ec2.allocate_address(Domain='vpc')
eip_id = eip['AllocationId']
static_ip = eip['PublicIp']

ec2.associate_address(
    InstanceId=instance_id,
    AllocationId=eip_id
)
print(f"ELASTIC_IP:{static_ip}")

# ──────────────────────────────────────────────
# SUMMARY
# ──────────────────────────────────────────────
print("\n" + "="*50)
print("HELIXONA AGENT EC2 — DEPLOYMENT COMPLETE")
print("="*50)
print(f"  Instance ID:  {instance_id}")
print(f"  Static IP:    {static_ip}")
print(f"  SSH:          ssh -i {key_path} ubuntu@{static_ip}")
print(f"  Instance:     {INSTANCE_TYPE} (2 vCPU, 8GB RAM)")
print(f"  IAM Role:     {ROLE_NAME}")
print(f"  Region:       {REGION}")
print("="*50)
print("The instance is auto-installing Python, Playwright, and Chromium.")
print("Wait ~3 minutes, then SSH in and deploy your code.")

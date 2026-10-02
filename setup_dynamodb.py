"""
Creates the four DynamoDB tables for the Helixona claim lifecycle:
  - Claims, Submissions, Adjudications, Tasks
"""
import boto3
import os
from dotenv import load_dotenv

load_dotenv()

session = boto3.Session(
    aws_access_key_id=os.environ['AWS_ACCESS_KEY_ID'],
    aws_secret_access_key=os.environ['AWS_SECRET_ACCESS_KEY'],
    region_name=os.environ.get('AWS_REGION', 'us-west-2')
)
dynamodb = session.client('dynamodb')

tables = [
    {
        "TableName": "helixona-claims",
        "KeySchema": [{"AttributeName": "claim_id", "KeyType": "HASH"}],
        "AttributeDefinitions": [
            {"AttributeName": "claim_id", "AttributeType": "S"},
            {"AttributeName": "current_state", "AttributeType": "N"},
            {"AttributeName": "patient_id", "AttributeType": "S"},
        ],
        "GlobalSecondaryIndexes": [
            {
                "IndexName": "state-index",
                "KeySchema": [{"AttributeName": "current_state", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "ALL"},
            },
            {
                "IndexName": "patient-index",
                "KeySchema": [{"AttributeName": "patient_id", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "ALL"},
            },
        ],
        "BillingMode": "PAY_PER_REQUEST",
    },
    {
        "TableName": "helixona-submissions",
        "KeySchema": [{"AttributeName": "submission_id", "KeyType": "HASH"}],
        "AttributeDefinitions": [
            {"AttributeName": "submission_id", "AttributeType": "S"},
            {"AttributeName": "claim_id", "AttributeType": "S"},
        ],
        "GlobalSecondaryIndexes": [
            {
                "IndexName": "claim-index",
                "KeySchema": [{"AttributeName": "claim_id", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "ALL"},
            },
        ],
        "BillingMode": "PAY_PER_REQUEST",
    },
    {
        # One item per Check/EFT the EOB bot (Remittance) captured from Blue Shield:
        # transaction summary, the claims it paid, the EOB report in S3.
        "TableName": "helixona-eobs",
        "KeySchema": [{"AttributeName": "check_eft", "KeyType": "HASH"}],
        "AttributeDefinitions": [
            {"AttributeName": "check_eft", "AttributeType": "S"},
        ],
        "BillingMode": "PAY_PER_REQUEST",
    },
    {
        # One item per check number the reconciliation knows: the image we
        # hold (SharePoint / S3 inbox), Blue Shield's status, eCW's payment,
        # and the verdict. Created on first use by src/checks/run.py too.
        "TableName": "helixona-checks",
        "KeySchema": [{"AttributeName": "check_number", "KeyType": "HASH"}],
        "AttributeDefinitions": [
            {"AttributeName": "check_number", "AttributeType": "S"},
        ],
        "BillingMode": "PAY_PER_REQUEST",
    },
    {
        # One item per denied claim: what eCW shows, the payer's reason codes
        # read off the claim, and the SOP cheat sheet's action for it.
        # Created on first use by src/denials/run.py too.
        "TableName": "helixona-denials",
        "KeySchema": [{"AttributeName": "claim_id", "KeyType": "HASH"}],
        "AttributeDefinitions": [
            {"AttributeName": "claim_id", "AttributeType": "S"},
        ],
        "BillingMode": "PAY_PER_REQUEST",
    },
    {
        # The Claude credits meter: _total, day:YYYY-MM-DD and _credits rows
        # with tokens and dollars per model (docs/claude_credits.md).
        # Created on first use by src/usage.py too.
        "TableName": "helixona-usage",
        "KeySchema": [{"AttributeName": "k", "KeyType": "HASH"}],
        "AttributeDefinitions": [
            {"AttributeName": "k", "AttributeType": "S"},
        ],
        "BillingMode": "PAY_PER_REQUEST",
    },
    {
        # Unlocked-notes notices: notice:<claim>:<ts>, weekly:<date>, _last
        # (docs/unlocked_notes.md). Created on first use by src/notes/unlocked.py too.
        "TableName": "helixona-notices",
        "KeySchema": [{"AttributeName": "k", "KeyType": "HASH"}],
        "AttributeDefinitions": [
            {"AttributeName": "k", "AttributeType": "S"},
        ],
        "BillingMode": "PAY_PER_REQUEST",
    },
    {
        "TableName": "helixona-adjudications",
        "KeySchema": [{"AttributeName": "claim_id", "KeyType": "HASH"}],
        "AttributeDefinitions": [
            {"AttributeName": "claim_id", "AttributeType": "S"},
        ],
        "BillingMode": "PAY_PER_REQUEST",
    },
    {
        "TableName": "helixona-tasks",
        "KeySchema": [{"AttributeName": "task_id", "KeyType": "HASH"}],
        "AttributeDefinitions": [
            {"AttributeName": "task_id", "AttributeType": "S"},
            {"AttributeName": "claim_id", "AttributeType": "S"},
        ],
        "GlobalSecondaryIndexes": [
            {
                "IndexName": "claim-index",
                "KeySchema": [{"AttributeName": "claim_id", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "ALL"},
            },
        ],
        "BillingMode": "PAY_PER_REQUEST",
    },
]

for table_def in tables:
    name = table_def["TableName"]
    try:
        dynamodb.describe_table(TableName=name)
        print(f"TABLE_EXISTS:{name}")
    except dynamodb.exceptions.ResourceNotFoundException:
        dynamodb.create_table(**table_def)
        print(f"TABLE_CREATED:{name}")
    except Exception as e:
        print(f"ERROR:{name}:{e}")

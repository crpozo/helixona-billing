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

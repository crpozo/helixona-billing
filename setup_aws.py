import boto3
import os
import uuid
from dotenv import load_dotenv

load_dotenv()

try:
    session = boto3.Session(
        aws_access_key_id=os.environ['AWS_ACCESS_KEY_ID'],
        aws_secret_access_key=os.environ['AWS_SECRET_ACCESS_KEY'],
        region_name=os.environ.get('AWS_REGION', 'us-west-2')
    )

    s3 = session.client('s3')
    bucket_name = f"helixona-claims-docs-{str(uuid.uuid4())[:8]}"
    s3.create_bucket(
        Bucket=bucket_name,
        CreateBucketConfiguration={'LocationConstraint': os.environ.get('AWS_REGION', 'us-west-2')}
    )
    print(f"BUCKET_CREATED:{bucket_name}")

    sqs = session.client('sqs')
    resp = sqs.create_queue(QueueName='helixona-agent-tasks')
    print(f"QUEUE_CREATED:{resp['QueueUrl']}")

    resp_iv = sqs.create_queue(QueueName='helixona-agent-iv-tasks')
    print(f"QUEUE_CREATED:{resp_iv['QueueUrl']}")
    
except Exception as e:
    print(f"ERROR:{e}")

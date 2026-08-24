import boto3
import json
import time
from src.config import settings
from src.utils.logger import get_logger

logger = get_logger(__name__)

# BOT_ROLE → the queue that role consumes. Stated as data so adding a bot is a
# one-line change and an unmapped role is impossible to miss.
QUEUE_BY_ROLE = {
    'submissions':   lambda s: s.sqs_queue_url,
    'resubmissions': lambda s: s.sqs_queue_url_resub,
}

ROLE_ENV_VAR = {
    'submissions':   'SQS_QUEUE_URL',
    'resubmissions': 'SQS_QUEUE_URL_RESUB',
}


class AWSClient:
    def __init__(self):
        self.region = settings.aws_region
        self.session = boto3.Session(region_name=self.region)
        self.sqs = self.session.client('sqs')
        self.s3 = self.session.client('s3')
        self.secrets = self.session.client('secretsmanager')
        self.dynamodb = self.session.resource('dynamodb')

    def get_secret(self, secret_name: str) -> dict:
        full_name = f"{settings.secrets_manager_prefix}{secret_name}"
        try:
            response = self.secrets.get_secret_value(SecretId=full_name)
            return json.loads(response['SecretString'])
        except Exception as e:
            logger.error(f"Failed to fetch secret {full_name}: {e}")
            raise

    def _active_queue_url(self) -> str:
        """Queue this process polls — determined by BOT_ROLE.

        Every role maps to its own queue explicitly. There is deliberately no
        fallback: an unrecognised or unconfigured role polls NOTHING rather
        than defaulting to the submissions queue.

        The old code fell through to `sqs_queue_url` for anything that was not
        'iv_corrections', so the resubmissions bot — which runs with
        BOT_ROLE=resubmissions and its own display, browser profile and noVNC —
        silently polled the submissions queue and competed with the submissions
        bot for the same messages, while its own queue was never read by
        anyone. Two bots racing for one queue is far worse than one bot idling.
        """
        queue = QUEUE_BY_ROLE.get(settings.bot_role)
        if queue is None:
            logger.error(
                f"BOT_ROLE={settings.bot_role!r} is not a known role "
                f"({', '.join(sorted(QUEUE_BY_ROLE))}). This process will not "
                f"poll any queue."
            )
            return ''
        url = queue(settings)
        if not url:
            logger.error(
                f"BOT_ROLE={settings.bot_role!r} has no queue URL configured "
                f"(expected {ROLE_ENV_VAR[settings.bot_role]} in .env). This "
                f"process will not poll any queue."
            )
        return url

    def receive_sqs_messages(self, max_messages=10, wait_time=20):
        url = self._active_queue_url()
        if not url:
            # Already logged by _active_queue_url. Idle rather than calling SQS
            # with an empty URL and spinning on the resulting error.
            time.sleep(wait_time)
            return []
        try:
            response = self.sqs.receive_message(
                QueueUrl=url,
                MaxNumberOfMessages=max_messages,
                WaitTimeSeconds=wait_time,
                AttributeNames=['All']
            )
            return response.get('Messages', [])
        except Exception as e:
            logger.error(f"Error receiving SQS messages: {e}")
            return []

    def delete_sqs_message(self, receipt_handle: str):
        try:
            self.sqs.delete_message(
                QueueUrl=self._active_queue_url(),
                ReceiptHandle=receipt_handle
            )
        except Exception as e:
            logger.error(f"Failed to delete SQS message: {e}")

    def upload_to_s3(self, file_path: str, object_name: str):
        try:
            self.s3.upload_file(file_path, settings.s3_bucket_name, object_name)
            logger.info(f"Uploaded {file_path} to s3://{settings.s3_bucket_name}/{object_name}")
            return f"s3://{settings.s3_bucket_name}/{object_name}"
        except Exception as e:
            logger.error(f"Failed to upload {file_path} to S3: {e}")
            raise
            
    # DynamoDB Methods
    def update_claim_status(self, claim_id: str, updates: dict):
        """Updates a claim record in the helixona-claims table.
        
        Handles DynamoDB reserved keywords (e.g. 'state', 'status', 'name')
        by mapping them through ExpressionAttributeNames.
        """
        try:
            table = self.dynamodb.Table('helixona-claims')
            
            # Build update expression with aliases for reserved keywords
            # DynamoDB reserved words: state, status, name, data, etc.
            expr_parts = []
            expr_values = {}
            expr_names = {}
            
            for k, v in updates.items():
                alias = f"#attr_{k}"
                value_key = f":{k}"
                expr_names[alias] = k
                expr_values[value_key] = v
                expr_parts.append(f"{alias} = {value_key}")
            
            update_expr = "set " + ", ".join(expr_parts)
            
            table.update_item(
                Key={'claim_id': claim_id},
                UpdateExpression=update_expr,
                ExpressionAttributeValues=expr_values,
                ExpressionAttributeNames=expr_names
            )
            logger.info(f"Updated claim {claim_id} in DynamoDB: {updates}")
        except Exception as e:
            logger.error(f"DynamoDB update failed for {claim_id}: {e}")
            raise

    def invoke_bedrock_model(self, prompt: str, system: str = "") -> str:
        """Invokes Claude 3 on Amazon Bedrock."""
        try:
            bedrock_runtime = self.session.client('bedrock-runtime')
            body = json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 1000,
                "system": system,
                "messages": [
                    {
                        "role": "user",
                        "content": prompt
                    }
                ]
            })
            response = bedrock_runtime.invoke_model(
                modelId='anthropic.claude-3-sonnet-20240229-v1:0', # Replace with required version
                contentType='application/json',
                accept='application/json',
                body=body
            )
            response_body = json.loads(response.get('body').read())
            return response_body['content'][0]['text']
        except Exception as e:
            logger.error(f"Bedrock invocation failed: {e}")
            raise

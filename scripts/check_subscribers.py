import boto3
table = boto3.resource('dynamodb', region_name='us-west-2').Table('helixona-claims')
resp = table.scan()
for item in sorted(resp['Items'], key=lambda x: x.get('claim_id', '')):
    sub = item.get('subscriber_id', '')
    sub_type = item.get('submission_type', '')
    if sub or sub_type == 'First Time Submission':
        cid = item['claim_id']
        print(f"  {cid:>6}  {sub_type:<25}  subscriber_id={sub or 'NOT CAPTURED'}")

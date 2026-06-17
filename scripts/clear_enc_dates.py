import boto3

table = boto3.resource('dynamodb', region_name='us-west-2').Table('helixona-claims')

# Scan all claims and remove wrong encounter_date values
resp = table.scan()
cleared = 0
for item in resp['Items']:
    if item.get('encounter_date'):
        table.update_item(
            Key={'claim_id': item['claim_id']},
            UpdateExpression='REMOVE encounter_date'
        )
        cleared += 1
        print(f"  Cleared encounter_date from claim {item['claim_id']}")

print(f"\nCleared {cleared} wrong encounter dates")

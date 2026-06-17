import boto3
table = boto3.resource('dynamodb', region_name='us-west-2').Table('helixona-claims')

item = table.get_item(Key={'claim_id': '1380'}).get('Item', {})
print('Claim 1380:')
print(f"  encounter_date: {item.get('encounter_date', 'MISSING')}")
print(f"  service_date:   {item.get('service_date', 'MISSING')}")
print(f"  subscriber_id:  {item.get('subscriber_id', 'MISSING')}")
print(f"  patient_name:   {item.get('patient_name', 'MISSING')}")

resp = table.scan()
print('\nAll encounter dates captured:')
for it in sorted(resp['Items'], key=lambda x: x.get('claim_id','')):
    ed = it.get('encounter_date')
    sd = it.get('service_date', '')
    if ed:
        print(f"  {it['claim_id']:>6}  enc_date={ed}  service_date={sd}  patient={it.get('patient_name','')}")

# Check which claims are still missing encounter_date
print('\nClaims missing encounter_date:')
missing = 0
for it in sorted(resp['Items'], key=lambda x: x.get('claim_id','')):
    if not it.get('encounter_date'):
        missing += 1
        print(f"  {it['claim_id']:>6}  patient={it.get('patient_name','')}  page={it.get('page_num','?')}")
print(f"Total missing: {missing}")

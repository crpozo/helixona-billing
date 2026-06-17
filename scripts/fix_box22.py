"""Bulk fix Box 22 data in DynamoDB by re-extracting from actual HCFA PDFs in S3."""
import pdfplumber, boto3, os

s3 = boto3.client("s3")
dynamodb = boto3.resource("dynamodb", region_name="us-west-2")
table = dynamodb.Table("helixona-claims")
bucket = "helixona-claims-docs-eb2f8e3c"

resp = s3.list_objects_v2(Bucket=bucket, Prefix="hcfa_forms/")
fixed = 0
for obj in resp.get("Contents", []):
    key = obj["Key"]
    cid = key.replace("hcfa_forms/", "").replace("_hcfa.pdf", "")
    
    if obj["Size"] < 500000:
        print(f"{cid:>6}  SKIPPED (damaged, {obj['Size']} bytes)")
        continue
    
    path = "/tmp/fix_box22.pdf"
    s3.download_file(bucket, key, path)
    
    original_ref = None
    resub_code = None
    
    with pdfplumber.open(path) as pdf:
        pg = pdf.pages[0]
        words = pg.extract_words()
        for w in words:
            x0 = float(w["x0"])
            top = float(w["top"])
            val = w["text"].strip()
            if 458 <= top <= 480:
                if 365 <= x0 <= 425 and val.isdigit() and len(val) <= 2:
                    resub_code = val
                if 435 <= x0 <= 555 and len(val) >= 8 and val.isdigit():
                    original_ref = val
    
    if original_ref:
        sub_type = "Resubmission"
        table.update_item(
            Key={"claim_id": cid},
            UpdateExpression="SET submission_type = :st, original_ref_no = :ref, resubmission_code = :rc",
            ExpressionAttributeValues={
                ":st": sub_type,
                ":ref": original_ref,
                ":rc": resub_code or "",
            },
        )
    else:
        sub_type = "First Time Submission"
        table.update_item(
            Key={"claim_id": cid},
            UpdateExpression="SET submission_type = :st REMOVE original_ref_no, resubmission_code",
            ExpressionAttributeValues={":st": sub_type},
        )
    
    print(f"{cid:>6}  {sub_type:<25}  ref={str(original_ref):>15}  code={resub_code}")
    fixed += 1

print(f"\nFixed {fixed} claims")

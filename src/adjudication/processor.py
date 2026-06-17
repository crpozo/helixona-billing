"""
Stage 8: Adjudication Outcome Processing
Handles EOB/ERA ingestion, FAIR Health comparison, and appeal auto-generation.
"""
from datetime import datetime
from src.utils.logger import get_logger
from src.rules.engine import RulesEngine

logger = get_logger(__name__)

rules = RulesEngine()


def process_adjudication(claim_id: str, eob_data: dict, aws_client) -> dict:
    """
    Processes an EOB/ERA for a given claim.
    eob_data keys:
        eob_date, allowed_amount, paid_amount, denial_codes,
        payment_recipient, fair_health_benchmark, recoupment_detected
    """
    logger.info(f"Processing adjudication for claim {claim_id}")

    allowed = eob_data.get('allowed_amount', 0)
    paid = eob_data.get('paid_amount', 0)
    benchmark = eob_data.get('fair_health_benchmark', 0)
    denial_codes = eob_data.get('denial_codes', [])

    # Determine outcome
    if paid > 0 and not denial_codes:
        outcome = 'Paid'
    elif paid > 0 and paid < allowed:
        outcome = 'Reduced'
    else:
        outcome = 'Denied'

    logger.info(f"Adjudication outcome: {outcome} (Paid: ${paid}, Allowed: ${allowed}, Benchmark: ${benchmark})")

    adjudication = {
        'claim_id': claim_id,
        'eob_date': eob_data.get('eob_date', datetime.now().isoformat()),
        'allowed_amount': allowed,
        'paid_amount': paid,
        'denial_codes': denial_codes,
        'payment_recipient': eob_data.get('payment_recipient', 'Unknown'),
        'outcome': outcome,
        'fair_health_benchmark': benchmark,
        'appeal_required': outcome != 'Paid',
        'recoupment_detected': eob_data.get('recoupment_detected', False),
    }

    # Run Rules 9 & 10
    rule_results = rules.validate_adjudication(adjudication)

    if outcome == 'Paid':
        # Post payment, reconcile, close claim (state 16)
        logger.info(f"Claim {claim_id}: PAID — posting payment and closing.")
        aws_client.update_claim_status(claim_id, {
            'current_state': 13,  # State 13: Paid
            'adjudication_outcome': outcome,
            'paid_amount': str(paid),
        })
        # After reconciliation → State 16
        aws_client.update_claim_status(claim_id, {'current_state': 16})
    else:
        # Reduced or Denied — generate appeal task (ZERO write-offs policy)
        logger.warning(f"Claim {claim_id}: {outcome} — generating appeal task (no write-offs)")

        aws_client.update_claim_status(claim_id, {
            'current_state': 14,  # State 14: Reduced/Denied
            'adjudication_outcome': outcome,
        })

        # FAIR Health comparison
        if benchmark > 0:
            underpayment = benchmark - paid
            logger.info(f"FAIR Health comparison: Benchmark ${benchmark} vs Paid ${paid} = Underpayment ${underpayment}")

        # Auto-generate appeal task
        appeal_task = {
            'claim_id': claim_id,
            'task_type': 'Appeal',
            'due_date': datetime.now().isoformat(),
            'owner': 'billing_team',
            'status': 'Open',
            'notes': f"Outcome: {outcome}. Denial codes: {denial_codes}. Underpayment vs FAIR Health: ${benchmark - paid if benchmark else 'N/A'}",
        }

        # Write task to DynamoDB
        import uuid
        task_id = str(uuid.uuid4())
        table = aws_client.dynamodb.Table('helixona-tasks')
        table.put_item(Item={**appeal_task, 'task_id': task_id})
        logger.info(f"Appeal task created: {task_id}")

        # Move to State 15: Appeal in Progress
        aws_client.update_claim_status(claim_id, {'current_state': 15})

    return adjudication

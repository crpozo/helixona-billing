"""
Rules Engine — v1 Blue Shield Specific
Implements all 10 business rules defined in the spec.
"""
from src.utils.logger import get_logger

logger = get_logger(__name__)


class RulesEngine:
    """Evaluates v1 Blue Shield business rules against claim data."""

    @staticmethod
    def rule_1_fee_schedule(claim: dict) -> dict:
        """Rule 1: Fee Schedule Enforcement — BS is ALWAYS OON"""
        if claim.get('fee_schedule') != 'OON':
            logger.warning(f"Rule 1 TRIGGERED: Fee schedule was '{claim.get('fee_schedule')}', auto-correcting to OON")
            claim['fee_schedule'] = 'OON'
            return {"rule": 1, "action": "auto_corrected", "field": "fee_schedule", "new_value": "OON"}
        return {"rule": 1, "action": "passed"}

    @staticmethod
    def rule_2_line_count(claim: dict) -> dict:
        """Rule 2: Line Count Validation"""
        claim_type = claim.get('claim_type', '')
        line_count = claim.get('line_count', 0)

        if claim_type == 'IV' and line_count <= 6:
            logger.warning(f"Rule 2 TRIGGERED: IV claim has {line_count} lines (need >6)")
            return {"rule": 2, "action": "flag_manual_review", "reason": f"IV claim with only {line_count} lines"}
        if claim_type in ('Office', 'Follow-up', 'Office/Follow-up') and line_count >= 6:
            logger.warning(f"Rule 2 TRIGGERED: Office claim has {line_count} lines (need <6)")
            return {"rule": 2, "action": "flag_manual_review", "reason": f"Office claim with {line_count} lines"}
        return {"rule": 2, "action": "passed"}

    @staticmethod
    def rule_3_icd_pointers(claim: dict) -> dict:
        """Rule 3: ICD Pointer Completeness — block if missing"""
        pointers = claim.get('icd_pointers', [])
        if not pointers or len(pointers) < 3:
            logger.error(f"Rule 3 TRIGGERED: ICD pointers incomplete ({pointers})")
            return {"rule": 3, "action": "block_submission", "reason": "ICD pointers incomplete", "task_type": "Manual Fix"}
        return {"rule": 3, "action": "passed"}

    @staticmethod
    def rule_4_waystar_gate(claim: dict) -> dict:
        """Rule 4: Waystar Acceptance Gate"""
        status = claim.get('waystar_status', '')
        if status == 'Rejected':
            logger.error("Rule 4 TRIGGERED: Waystar rejected claim")
            return {"rule": 4, "action": "route_exception", "reason": "Waystar rejected"}
        return {"rule": 4, "action": "passed"}

    @staticmethod
    def rule_5_bs_claim_required(claim: dict) -> dict:
        """Rule 5: BS Claim # Required Before Documentation"""
        if not claim.get('blueshield_claim_number'):
            logger.warning("Rule 5 TRIGGERED: BS Claim # is null — holding documentation")
            return {"rule": 5, "action": "hold", "reason": "BS Claim # not yet captured"}
        return {"rule": 5, "action": "passed"}

    @staticmethod
    def rule_6_ai_verification_gate(claim: dict) -> dict:
        """Rule 6: AI Medical Record Verification Gate"""
        if not claim.get('medical_record_verified', False):
            logger.error("Rule 6 TRIGGERED: Medical record NOT verified by AI")
            return {"rule": 6, "action": "block_symplisend", "reason": "AI verification failed", "task_type": "Exception Review"}
        return {"rule": 6, "action": "passed"}

    @staticmethod
    def rule_7_itemized_bill(submission: dict) -> dict:
        """Rule 7: Itemized Bill Required for Itemization Submission"""
        if submission.get('submission_type') == 'Itemization' and not submission.get('is_itemized_bill', False):
            logger.error("Rule 7 TRIGGERED: Itemization without Itemized Bill")
            return {"rule": 7, "action": "block", "reason": "Itemized Bill required but missing"}
        return {"rule": 7, "action": "passed"}

    @staticmethod
    def rule_8_fln_capture(submission: dict) -> dict:
        """Rule 8: FLN# Capture Confirmation"""
        if submission.get('status') == 'Completed' and not submission.get('fln_number'):
            logger.error("Rule 8 TRIGGERED: SympliSend completed but FLN# is missing!")
            return {"rule": 8, "action": "alert", "reason": "FLN# not captured on completed submission"}
        return {"rule": 8, "action": "passed"}

    @staticmethod
    def rule_9_no_writeoffs(adjudication: dict) -> dict:
        """Rule 9: No Write-Offs on Adjudication — zero passive write-offs"""
        outcome = adjudication.get('outcome', '')
        if outcome and outcome != 'Paid':
            logger.warning(f"Rule 9 TRIGGERED: Outcome is '{outcome}' — generating appeal task")
            return {"rule": 9, "action": "generate_appeal", "reason": f"Non-Paid outcome: {outcome}", "task_type": "Appeal"}
        return {"rule": 9, "action": "passed"}

    @staticmethod
    def rule_10_recoupment(adjudication: dict) -> dict:
        """Rule 10: Recoupment Detection"""
        if adjudication.get('recoupment_detected', False):
            logger.error("Rule 10 TRIGGERED: Recoupment detected!")
            return {"rule": 10, "action": "immediate_alert_and_appeal", "reason": "Recoupment detected", "task_type": "Appeal"}
        return {"rule": 10, "action": "passed"}

    def validate_claim_pre_submission(self, claim: dict) -> list:
        """Run Rules 1-6 before SympliSend submission."""
        results = [
            self.rule_1_fee_schedule(claim),
            self.rule_2_line_count(claim),
            self.rule_3_icd_pointers(claim),
            self.rule_4_waystar_gate(claim),
            self.rule_5_bs_claim_required(claim),
            self.rule_6_ai_verification_gate(claim),
        ]
        blocked = [r for r in results if r['action'] in ('block_submission', 'block_symplisend', 'route_exception', 'hold', 'flag_manual_review')]
        if blocked:
            logger.error(f"Claim blocked by {len(blocked)} rule(s): {[r['rule'] for r in blocked]}")
        else:
            logger.info("All pre-submission rules passed.")
        return results

    def validate_submission_post(self, submission: dict) -> list:
        """Run Rules 7-8 after SympliSend submission."""
        results = [
            self.rule_7_itemized_bill(submission),
            self.rule_8_fln_capture(submission),
        ]
        return results

    def validate_adjudication(self, adjudication: dict) -> list:
        """Run Rules 9-10 on adjudication outcome."""
        results = [
            self.rule_9_no_writeoffs(adjudication),
            self.rule_10_recoupment(adjudication),
        ]
        return results

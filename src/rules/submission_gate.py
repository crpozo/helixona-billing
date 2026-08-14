"""The gate deciding which claims may be transmitted to a payer.

This is the last check before patient documentation leaves the building, so it
is the one piece of logic that most deserves to be pure, named and tested.

It used to live inline inside `process_message`, written out twice — once for
the batch loop and once for the single-claim test path. Two copies of a rule
drift, and these already had: the batch loop honoured the reviewer's
"progress note not required" override, while the test path's explanation of
*why* a claim was rejected ignored it and would report a missing Progress Note
for a claim that did not need one.

The rule
--------
1. HCFA is always required.
2. The IV Note is always required, and must not be flagged as belonging to a
   different patient.
3. The Progress Note is required for IV Therapy, and must not be flagged for
   revision. Office visits and claims a reviewer manually exempted skip it.
4. A subscriber ID is required, and must have been confirmed from HCFA box 1a
   — a DOM-scraped value alone is held back so an unconfirmed member number is
   never auto-submitted to a payer.
5. Already-submitted claims are reported separately, so a caller can choose to
   skip them (batch) or report them (single-claim test).
"""

import re

OFFICE_VISIT_CPTS = {
    '99201', '99202', '99203', '99204', '99205',
    '99211', '99212', '99213', '99214', '99215',
}


def is_office_visit(cpt_value) -> bool:
    """True if the claim's CPT contains an office-visit E/M code.

    `cpt_value` may be a single code, a comma/space separated list, or None.
    """
    if not cpt_value:
        return False
    codes = re.findall(r'\d{5}', str(cpt_value))
    return any(c in OFFICE_VISIT_CPTS for c in codes)


def evaluate_claim(claim: dict) -> dict:
    """Decide whether a claim's documentation is complete enough to transmit.

    Returns:
        ready             — documentation and identity requirements are met.
                            Deliberately independent of `already_submitted`,
                            so the batch path can skip resends while the
                            single-claim path can still say "this one is fine,
                            it just went already".
        already_submitted — a packet has previously gone out for this claim.
        blockers          — human-readable reasons `ready` is False.
        progress_note_exempt — whether rule 3 was waived, and why.
    """
    has_hcfa = bool(claim.get('hcfa_s3_path'))
    has_iv_note = bool(claim.get('prog_notes_s3_path'))
    has_progress_note = bool(claim.get('encounter_file_s3_path'))

    subscriber = claim.get('subscriber_id')
    subscriber_unverified = bool(claim.get('subscriber_id_unverified'))
    has_subscriber = bool(subscriber) and not subscriber_unverified

    iv_note_mismatch = bool(claim.get('iv_note_patient_mismatch'))
    progress_note_needs_review = bool(claim.get('encounter_revision_needed'))

    office = is_office_visit(claim.get('cpt'))
    manual_exempt = bool(claim.get('progress_note_not_required'))
    pn_exempt = office or manual_exempt

    blockers = []
    if not has_hcfa:
        blockers.append('HCFA')
    if not has_iv_note:
        blockers.append('IV Note')
    if iv_note_mismatch:
        blockers.append('IV Note has patient mismatch')
    if not pn_exempt:
        if not has_progress_note:
            blockers.append('Progress Note')
        if progress_note_needs_review:
            blockers.append('Progress Note needs review')
    if not has_subscriber:
        if subscriber and subscriber_unverified:
            blockers.append('Subscriber ID UNVERIFIED (HCFA box 1a not read)')
        else:
            blockers.append('Subscriber ID')

    return {
        'ready': not blockers,
        'already_submitted': bool(claim.get('symplisend_submitted')),
        'blockers': blockers,
        'progress_note_exempt': (
            'office visit' if office else 'reviewer override' if manual_exempt else ''
        ),
    }


def ready_to_submit(claim: dict) -> bool:
    """Batch-path convenience: complete documentation AND not yet sent."""
    verdict = evaluate_claim(claim)
    return verdict['ready'] and not verdict['already_submitted']

"""Which SympliSend form a claim belongs in, and what it needs filled.

SympliSend has one form per submission type, chosen from the top navigation:

    Provider First Submission Claim   a claim the payer has not seen
    Provider Prior Claim Submission   documents for a claim already on file

They are not interchangeable. The prior-claim form has a required **Claim
Number** field; without it the payer opens a new claim rather than attaching
the documents to the one under dispute — which is how 803 resubmissions came
to be reported as never received while our own log said they were sent.

A claim's own record says which it is: `submission_type` carries the
classification read from HCFA box 22, and `original_ref_no` carries the prior
claim number that box 22 cites.
"""

FIRST_SUBMISSION = 'Provider First Submission Claim'
PRIOR_CLAIM = 'Provider Prior Claim Submission'

# "Type of Attachment" on the prior-claim form. Blue Shield denied these for
# missing medical records, so what we are sending is the records they asked
# for.
ATTACHMENT_BS_REQUESTED = 'BS Requested Records'
ATTACHMENT_CORRECTED = 'Corrected Claim'


def is_resubmission(claim: dict) -> bool:
    return 'resub' in str(claim.get('submission_type', '')).lower()


def prior_claim_number(claim: dict) -> str:
    return str(claim.get('original_ref_no', '') or '').strip()


def form_for(claim: dict) -> str:
    """The SympliSend form this claim should be submitted through.

    A resubmission without a prior claim number cannot use the prior-claim
    form — the field is required — so it falls back to a first submission,
    which is what has been happening to every resubmission until now. The
    caller is expected to notice and record it.
    """
    if is_resubmission(claim) and prior_claim_number(claim):
        return PRIOR_CLAIM
    return FIRST_SUBMISSION


def attachment_type_for(claim: dict) -> str:
    """Only the prior-claim form asks for this."""
    return ATTACHMENT_BS_REQUESTED


def describe(claim: dict) -> dict:
    """Everything the submission step needs to fill the right form."""
    form = form_for(claim)
    return {
        'form': form,
        'subscriber_id': str(claim.get('subscriber_id', '') or ''),
        'claim_number': prior_claim_number(claim) if form == PRIOR_CLAIM else '',
        'attachment_type': attachment_type_for(claim) if form == PRIOR_CLAIM else '',
        'is_medicare': False,
        'is_heat_claim': False,
        # A resubmission we cannot route to the prior-claim form. Not an error
        # that should stop the send, but it must not pass unrecorded.
        'downgraded': is_resubmission(claim) and not prior_claim_number(claim),
    }


# ── Which bot owns which claims ────────────────────────────────────────────
#
# The two bots are separate lanes: their own queue, browser profile, display
# and noVNC. The submission step, though, used to scan every claim in the table
# regardless of type, so either bot would send anything that was ready — the
# tab picked who did the work, not what work got done. Running both at once
# meant two browsers racing for the same claims.
_ROLE_OWNS = {
    'submissions': lambda c: not is_resubmission(c),
    'resubmissions': is_resubmission,
}


def belongs_to_bot(claim: dict, role: str) -> bool:
    """True if this claim is the given bot's work.

    An unrecognised role owns nothing. That matches how queues are routed:
    idling is safe, and picking up another lane's claims is not.
    """
    owns = _ROLE_OWNS.get(role)
    return bool(owns(claim)) if owns else False

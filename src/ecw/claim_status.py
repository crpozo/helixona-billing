"""Which ECW claim status each bot pulls its work from.

ECW carries the new-submission / resubmission split in the claim status itself,
and the two bots divide along it:

    Ready to Submit to Symplisend   → first-time submissions   (helixona-agent)
    Ready to Bill - Symplisend CC   → resubmissions            (helixona-agent-resub)

The correlation in production is near total — of 774 claims classified as a
Resubmission, 773 sit in "Ready to Bill - Symplisend CC"; of 424 first-time
submissions, 412 sit in "Ready to Submit to Symplisend".

The extractor used to hardcode "Ready to Submit to Symplisend" for every bot,
so claims in "Ready to Bill - Symplisend CC" were invisible to it no matter how
many were waiting — 785 of them at the time this was written. A run would
report success having looked at only half the board.
"""

ECW_STATUS_BY_ROLE = {
    'submissions':   'Ready to Submit to Symplisend',
    'resubmissions': 'Ready to Bill - Symplisend CC',
}

DEFAULT_ECW_STATUS = 'Ready to Submit to Symplisend'


def ecw_status_for(role: str) -> str:
    """The ECW claim-status filter this bot should select.

    An unknown role falls back to the first-time-submission status — the
    conservative direction, since that is the queue that was always being
    extracted and no new class of claim enters the pipeline by surprise.
    """
    return ECW_STATUS_BY_ROLE.get(role, DEFAULT_ECW_STATUS)

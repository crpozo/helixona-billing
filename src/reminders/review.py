"""The review list — what the Reminders bot is about to send, and to whom.

One workbook a person reads before anyone presses *Send the reminders*:
who gets a message and at which address, who has no address and must be
called, which checks are too new to chase, and the message itself, as each
patient would receive it. Built from the same rows and the same template
the sending uses, so what is reviewed is what goes out.
"""
import io
from datetime import date, timedelta

from src.reminders.run import _date, compose

SEND_COLS = ['Patient', 'E-mail', 'Phone', 'Check #', 'Check date', 'Amount', 'Date of service', 'eCW claim', 'Blue Shield status',
             'Cashed by patient', 'Card on file', 'Notice', 'Deadline in the message', 'Subject', 'Message']
CALL_COLS = ['Patient', 'Phone', 'Check #', 'Check date', 'Amount', 'Date of service', 'eCW claim', 'Blue Shield status',
             'Cashed by patient', 'Why no e-mail']
WAIT_COLS = ['Patient', 'Check #', 'Check date', 'Amount', 'Stage', 'Reminders sent', 'Last reminder', 'Deadline']
NOTICE = {'first': 'First notice', 'reminder': 'Reminder', 'final': 'Final notice'}


def _card(r):
    return {True: 'yes', False: 'NO'}.get(r.get('card_on_file'), 'not read')


def _amount(r):
    try:
        return float(r.get('amount') or 0)
    except (TypeError, ValueError):
        return 0.0


def review_lists(rows, policy, today=None):
    """(to_send, to_call, waiting, summary): the rows of each sheet."""
    today = today or date.today()
    to_send, to_call, waiting = [], [], []
    for r in rows:
        stage, action = r.get('stage'), r.get('next_action') or ''
        base = [r.get('patient', ''), r.get('check_number', ''), r.get('check_date', ''), _amount(r)]
        if stage == 'to remind' and action:
            deadline = _date(r.get('deadline')) or today + timedelta(days=int(policy.get('deadline_days', 14)))
            subject, text = compose(policy, r, deadline, action)
            claim = ', '.join(r.get('claim_ids') or [])
            if r.get('email'):
                to_send.append([r.get('patient', ''), r['email'], r.get('phone', ''), r.get('check_number', ''), r.get('check_date', ''),
                                _amount(r), r.get('dos', ''), claim, r.get('bs_status', ''), 'yes' if r.get('cashed') else 'no',
                                _card(r), NOTICE.get(action, action), deadline.strftime('%m/%d/%Y'), subject, text])
            else:
                why = ('not looked up yet — run "Find new checks and draft"' if not r.get('email_source') and claim
                       else 'no eCW claim matched to this check' if not claim else 'eCW has no e-mail for this patient')
                to_call.append([r.get('patient', ''), r.get('phone', ''), r.get('check_number', ''), r.get('check_date', ''), _amount(r),
                                r.get('dos', ''), claim, r.get('bs_status', ''), 'yes' if r.get('cashed') else 'no', why])
        elif stage in ('in the mail', 'reminded', 'deadline passed'):
            waiting.append(base + [stage, int(r.get('reminders_sent') or 0), r.get('last_reminder_at', ''), r.get('deadline', '')])
    to_send.sort(key=lambda x: (x[0], x[3]))
    to_call.sort(key=lambda x: (x[0], x[2]))
    summary = {
        'as_of': today.isoformat(), 'from': policy.get('from') or '(not set)',
        'to_send': len(to_send), 'to_send_amount': round(sum(x[5] for x in to_send), 2),
        'patients': len({x[1].lower() for x in to_send}),
        'to_call': len(to_call), 'to_call_amount': round(sum(x[4] for x in to_call), 2),
        'no_card': sum(1 for x in to_send if x[10] == 'NO'), 'cashed': sum(1 for x in to_send if x[9] == 'yes'),
        'waiting': len(waiting),
    }
    return to_send, to_call, waiting, summary


def samples(policy, today=None):
    """The message in each of its forms, on a made-up patient."""
    today = today or date.today()
    deadline = today + timedelta(days=int(policy.get('deadline_days', 14)))
    base = {'patient': 'DOE, JANE', 'check_number': '12345678', 'check_date': '11/02/2026', 'amount': '250.00', 'dos': '10/01/2026'}
    out = []
    for label, extra, action in (
            ('Check not cashed · card on file · first notice', {'cashed': False, 'card_on_file': True}, 'first'),
            ('Check not cashed · NO card on file', {'cashed': False, 'card_on_file': False}, 'first'),
            ('Check already cashed by the patient', {'cashed': True, 'card_on_file': True}, 'first'),
            ('Final notice', {'cashed': False, 'card_on_file': True}, 'final')):
        subject, text = compose(policy, {**base, **extra}, deadline, action)
        out.append((label, subject, text))
    return out


def review_workbook(rows, policy, today=None):
    """The .xlsx, as bytes."""
    import openpyxl
    from openpyxl.styles import Alignment, Font
    today = today or date.today()
    to_send, to_call, waiting, sm = review_lists(rows, policy, today)
    wb = openpyxl.Workbook()
    bold, wrap = Font(bold=True), Alignment(wrap_text=True, vertical='top')

    ws = wb.active
    ws.title = 'Summary'
    for line in (
            ('Blue Shield checks sent to patients — reminders for review', ''),
            ('As of', sm['as_of']), ('Sent from', sm['from']), ('', ''),
            ('Messages ready to send', sm['to_send']), ('  to this many different addresses', sm['patients']),
            ('  amount they cover', sm['to_send_amount']), ('  of them: the patient already cashed the check', sm['cashed']),
            ('  of them: no card on file (the message says "billed", not "charged")', sm['no_card']),
            ('No e-mail — to be called', sm['to_call']), ('  amount they cover', sm['to_call_amount']),
            ('Not due now (in the mail, already reminded, past the deadline)', sm['waiting']), ('', ''),
            ('The rules', ''),
            ('  First notice', f"once the check is {policy.get('mail_days', 7)} days old (or cashed)"),
            ('  Then', f"a reminder every {policy.get('reminder_every_days', 5)} days, {policy.get('max_reminders', 3)} messages at most; the last is the final notice"),
            ('  Deadline', f"{policy.get('deadline_days', 14)} days after the first notice; it does not move"),
            ('  After the deadline', 'no more messages; a person decides — the bot never charges a card'), ('', ''),
            ('Nothing has been sent. Messages go out only when a person runs "Send the reminders".', '')):
        ws.append(list(line))
    ws['A1'].font = Font(bold=True, size=14)
    ws.column_dimensions['A'].width, ws.column_dimensions['B'].width = 62, 60

    def sheet(title, cols, data, wide=()):
        s = wb.create_sheet(title)
        s.append(cols)
        for c in s[1]:
            c.font = bold
        for row in data:
            s.append(row)
        s.freeze_panes = 'A2'
        for i, name in enumerate(cols, 1):
            letter = openpyxl.utils.get_column_letter(i)
            s.column_dimensions[letter].width = 90 if name in wide else 34 if name in ('E-mail', 'Subject', 'Why no e-mail') else 24 if name == 'Patient' else 16
        if wide:
            for row in s.iter_rows(min_row=2):
                for c in row:
                    c.alignment = wrap
        return s

    sheet('To send', SEND_COLS, to_send, wide=('Message',))
    sheet('No e-mail - call', CALL_COLS, to_call)
    sheet('Not due now', WAIT_COLS, waiting)
    s = wb.create_sheet('Message wording')
    s.append(['Case', 'Subject', 'Message (made-up patient)'])
    for c in s[1]:
        c.font = bold
    for label, subject, text in samples(policy, today):
        s.append([label, subject, text])
    s.column_dimensions['A'].width, s.column_dimensions['B'].width, s.column_dimensions['C'].width = 40, 48, 100
    for row in s.iter_rows(min_row=2):
        for c in row:
            c.alignment = wrap
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()

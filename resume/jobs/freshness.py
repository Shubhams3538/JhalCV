"""Deterministic posting-date policy; never use discovery/update time as posting time."""
from datetime import date, timedelta

MAX_AGE_DAYS = 7


def posting_date(value):
    try:
        return date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        return None


def freshness(value, today=None):
    today = today or date.today()
    posted = posting_date(value)
    if posted is None or posted > today:
        return 'unknown'
    return 'recent' if posted >= today - timedelta(days=MAX_AGE_DAYS) else 'expired'


def newest_first(items):
    return sorted(items, key=lambda j: (freshness(j.get('posted')) == 'recent',
                                      posting_date(j.get('posted')) or date.min), reverse=True)


def category(job):
    """One authoritative category; Applied always wins over age and AI results."""
    if job['status'] == 'applied':
        return 'applied'
    age = freshness(job.get('posted'))
    if age == 'expired':
        return 'old'
    if job.get('manual_eligible') == 'no':
        return 'no'
    if job['status'] == 'hidden':
        return 'review'
    if job.get('eligible') == 'no' or job.get('relevant') == 'no':
        return 'no'
    if job.get('eligible') == 'yes' and job.get('relevant') == 'yes':
        # Some sites publish no posting date. A job that passes both checks is
        # still shown as Eligible; the list labels its date as unverified.
        return 'eligible'
    return 'review'

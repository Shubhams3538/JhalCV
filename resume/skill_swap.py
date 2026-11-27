"""Fit the job's own skill terms into the existing skill groups.

The model names the terms; this module decides what is allowed. A term must
appear in the job description, skill groups are never created, and pinned skills
and skills the job asks for are never removed. Which removals are needed is
decided against the real page in `workspace.fit_skills`, after the layout is
final, so a skill can never push a project bullet off the page.
"""
from __future__ import annotations

import re

from .insights import DISPLAY, canonical
from .tailor import _term_present

MAX_ADDED_SKILLS = 15
MAX_TERM_CHARS = 40
_TERM = re.compile(r'[A-Za-z0-9.+#][A-Za-z0-9+#./ -]*')

# Terms made only of these words describe a person or a job, not a skill.
GENERIC_TOKENS = set("""
    problem solving skill skills ability abilities experience knowledge understanding
    scalability resilience reliability availability maintainability performance quality
    excellence fundamentals principles practices methodologies patterns architecture
    design development engineering programming coding software systems backend frontend
    fullstack cloud security testing agile strong excellent good solid proven deep broad
""".split())
# One of these anywhere in a term is enough to reject it.
SOFT_TOKENS = set("""
    communication teamwork collaboration leadership ownership mentorship mentoring
    thinking mindset attitude passion motivation degree bachelors bachelor masters
    master phd years experience
""".split())


def _is_skill_term(term):
    if not _TERM.fullmatch(term) or len(term) > MAX_TERM_CHARS:
        return False
    tokens = term.lower().split()
    if not 1 <= len(tokens) <= 3 or SOFT_TOKENS.intersection(tokens):
        return False
    return not GENERIC_TOKENS.issuperset(tokens)


def pinned_skills(profile):
    pinned = profile.get('_locked', {}).get('pinned_skills', [])
    return {canonical(skill) for skill in pinned if isinstance(skill, str)}


def job_terms(raw, jd, groups):
    """Valid terms from the model's list, required first, one per skill."""
    blob = ' ' + jd.lower() + ' '
    found = {}
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, dict) or not isinstance(entry.get('term'), str):
            continue
        term = ' '.join(entry['term'].split())
        importance = 'required' if entry.get('importance') == 'required' else 'preferred'
        if not _is_skill_term(term) or not _term_present(term, blob):
            continue
        key = canonical(term)
        # Matching ignores case, so a job's lowercase spelling can read properly.
        if term.islower():
            term = DISPLAY.get(key, term[0].upper() + term[1:])
        known = found.get(key)
        if known is None or (importance == 'required' and known['importance'] != 'required'):
            group = entry.get('group')
            found[key] = {'term': term, 'importance': importance,
                          'group': group if group in groups else None}
    return sorted(found.values(), key=lambda item: item['importance'] != 'required')


def plan_swaps(profile, skills, raw_terms, jd, matched=()):
    """Describe every skill change for this job. Nothing is applied here."""
    groups = [group['label'] for group in skills]
    terms = job_terms(raw_terms, jd, groups)
    keys = {canonical(item['term']) for item in terms}
    locations = {canonical(skill): group['label'] for group in skills for skill in group['items']}
    blob = ' ' + jd.lower() + ' '
    additions, renames = [], []
    for item in terms:
        key = canonical(item['term'])
        if key in locations:
            current = next(skill for group in skills for skill in group['items'] if canonical(skill) == key)
            # Both spellings keep an exact-match search working for either one.
            if current.lower() != item['term'].lower() and not _term_present(current, blob):
                renames.append({'group': locations[key], 'from': current,
                                'to': f"{current} ({item['term']})"})
        elif item['group'] and len(additions) < MAX_ADDED_SKILLS:
            additions.append(item)
    return {'pinned': sorted(pinned_skills(profile)),
            'keep': sorted(keys | {canonical(skill) for skill in matched}),
            'keywords': [{'term': item['term'], 'importance': item['importance']} for item in terms],
            'additions': additions, 'renames': renames}


def expand(skills, plan):
    """Return every skill at once, plus removals in the order space should be freed.

    Least valuable goes first: skills the job never mentions, then preferred
    additions, then required additions. Pinned and job-named skills are absent.
    """
    protected = set(plan['pinned']) | set(plan['keep'])
    renames = {(rename['group'], rename['from']): rename['to'] for rename in plan['renames']}
    full, fillers = [], {}
    for group in skills:
        label = group['label']
        kept, spare = [], []
        for skill in group['items']:
            (kept if canonical(skill) in protected else spare).append(skill)
        added = [item['term'] for item in plan['additions'] if item['group'] == label]
        items = [renames.get((label, skill), skill) for skill in kept] + added + spare
        full.append({**group, 'items': items})
        fillers[label] = spare

    lengths = {group['label']: sum(len(item) + 2 for item in group['items']) for group in full}
    remaining = {group['label']: len(group['items']) for group in skills}
    removals = []
    # The longest line is the one most likely to shed a wrapped row. A group
    # keeps one saved item so losing an addition can never leave it empty.
    while True:
        open_groups = [label for label, spare in fillers.items() if spare and remaining[label] > 1]
        if not open_groups:
            break
        label = max(open_groups, key=lambda name: lengths[name])
        skill = fillers[label].pop()
        lengths[label] -= len(skill) + 2
        remaining[label] -= 1
        removals.append({'group': label, 'skill': skill})
    for item in reversed(plan['additions']):
        removals.append({'group': item['group'], 'skill': item['term']})
    return full, removals


def without(skills, removals):
    gone = {(removal['group'], removal['skill']) for removal in removals}
    result = []
    for group in skills:
        items = [skill for skill in group['items'] if (group['label'], skill) not in gone]
        if items:
            result.append({**group, 'items': items})
    return result


def _resume_text(data, skills):
    parts = [skill for group in skills for skill in group['items']]
    for section in ('experience', 'projects'):
        for entry in data.get(section, []):
            parts.extend(entry.get('stack', []))
            parts.extend(bullet.replace('**', '') for bullet in entry.get('bullets', []))
    return ' ' + ' \n '.join(parts).lower() + ' '


def _coverage(plan, data, skills):
    blob = _resume_text(data, skills)
    listed = {canonical(skill) for group in skills for skill in group['items']}
    result = {}
    for importance in ('required', 'preferred'):
        terms = [item['term'] for item in plan['keywords'] if item['importance'] == importance]
        missing = [term for term in terms if canonical(term) not in listed and not _term_present(term, blob)]
        result[importance] = {'covered': len(terms) - len(missing), 'total': len(terms), 'missing': missing}
    return result


def summarize(plan, data, before, after):
    """What changed on the page and how much of the job it now covers."""
    was = {(group['label'], skill) for group in before for skill in group['items']}
    now = {(group['label'], skill) for group in after for skill in group['items']}
    renamed = {(rename['group'], rename['to']): rename['from'] for rename in plan['renames']}
    renames = [{'from': old, 'to': new[1]} for new, old in renamed.items() if new in now]
    kept = {(new[0], old) for new, old in renamed.items() if new in now}
    return {'added': [skill for _, skill in sorted(now - was - set(renamed))],
            'removed': [skill for _, skill in sorted(was - now - kept)],
            'renamed': renames,
            'coverage_before': _coverage(plan, data, before),
            'coverage_after': _coverage(plan, data, after)}


def notice(summary):
    lines = []
    if summary['added']:
        lines.append('Skills added from the job: ' + ', '.join(summary['added']) + '.')
    if summary['removed']:
        lines.append('Skills removed to make room: ' + ', '.join(summary['removed']) + '.')
    if summary['renamed']:
        lines.append('Skills respelled to match the job: ' + ', '.join(item['to'] for item in summary['renamed']) + '.')
    labels = {'required': 'Required', 'preferred': 'Preferred'}
    before, after = summary['coverage_before'], summary['coverage_after']
    for importance, label in labels.items():
        if after[importance]['total']:
            line = (f"{label} job terms on the resume: {before[importance]['covered']} to "
                    f"{after[importance]['covered']} of {after[importance]['total']}.")
            if after[importance]['missing']:
                line += ' Still missing: ' + ', '.join(after[importance]['missing']) + '.'
            lines.append(line)
    return lines

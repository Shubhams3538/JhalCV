"""Conservative local resume suggestions; uncertain/missing facts stay editable."""
import re
from urllib.parse import urlsplit

HEADINGS = {
    'experience': 'experience', 'work experience': 'experience', 'professional experience': 'experience',
    'employment': 'experience', 'internships': 'experience', 'education': 'education',
    'academic qualifications': 'education', 'projects': 'projects', 'personal projects': 'projects',
    'technical projects': 'projects', 'skills': 'skills', 'technical skills': 'skills',
    'coding profiles': 'coding_profiles', 'coding profiles and achievements': 'coding_profiles',
    'achievements': 'coding_profiles', 'certifications': 'ignore', 'summary': 'ignore',
    'profile': 'ignore', 'objective': 'ignore', 'interests': 'ignore',
}
URL = re.compile(r'(?:https?://|www\.)[^\s<>"\]]+|(?:github\.com|linkedin\.com)/[^\s<>"\]]+', re.I)
DATE = r'(?:(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)[.,]?\s+)?(?:19|20)\d{2}'
RANGE = re.compile(r'(?P<start>' + DATE + r')\s*(?:–|—|\s-\s|(?<=\d)-(?=\d)|\bto\b)\s*(?P<end>' + DATE + r'|Present|Current|Now)', re.I)
BULLET = re.compile(r'^\s*[•●▪◦*-]\s+')
ROLE = re.compile(r'\b(engineer|developer|intern|analyst|manager|consultant|scientist|designer|architect|sde|swe|researcher|instructor|assistant|trainee)\b', re.I)


def columns(value):
    return [part.strip(' |') for part in re.split(r'\s{2,}|\s+\|\s+', value) if part.strip(' |')]


def web_links(content, extra=()):
    out = []
    for value in [*extra, *URL.findall(content)]:
        value = value.strip().rstrip('.,;|)')
        if not value.lower().startswith(('http://', 'https://')):
            value = 'https://' + value
        try:
            parsed = urlsplit(value)
        except ValueError:
            continue
        if parsed.scheme in ('http', 'https') and parsed.hostname and not parsed.username and not parsed.password and value not in out:
            out.append(value)
    return out[:100]


def suggested_profile(content, empty, extra_links=()):
    profile = empty()
    lines = [line.rstrip() for line in content.splitlines() if line.strip()]
    email = re.search(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}', content)
    # A telephone candidate must contain 8-15 digits and no year-range dash.
    phone = next((m.group().strip() for m in re.finditer(r'(?<!\w)\+?\d[\d ()-]{6,}\d', content)
                  if 8 <= len(re.sub(r'\D', '', m.group())) <= 15 and not RANGE.search(m.group())), '')
    profile['basics'].update(name=lines[0].strip()[:200] if lines else '', email=email.group() if email else '', phone=phone)
    links = web_links(content, extra_links)
    for url in links:
        parsed = urlsplit(url)
        host, path = (parsed.hostname or '').lower().removeprefix('www.'), parsed.path.strip('/')
        if host == 'github.com' and path and '/' not in path:
            profile['basics']['links'].append({'label': 'GitHub', 'url': url})
        elif host == 'linkedin.com' and path.startswith('in/'):
            profile['basics']['links'].append({'label': 'LinkedIn', 'url': url})
    sections, current = {}, None
    for line in lines:
        heading = re.sub(r'\s+', ' ', line.strip().rstrip(':')).casefold()
        if heading in HEADINGS:
            current = HEADINGS[heading]
        elif current and current != 'ignore':
            sections.setdefault(current, []).append(line)
    for section in ('experience', 'education', 'projects'):
        blocks, headers, bullets = [], [], []
        bullet_column = 0
        section_lines = sections.get(section, [])
        for position, line in enumerate(section_lines):
            match = BULLET.match(line)
            if match:
                bullets.append(line[match.end():].strip())
                bullet_column = match.end()
            elif bullets:
                following = section_lines[position + 1] if position + 1 < len(section_lines) else ''
                indentation = len(line) - len(line.lstrip())
                if indentation >= bullet_column and not RANGE.search(line) and not RANGE.search(following):
                    bullets[-1] += ' ' + line.strip()
                else:
                    blocks.append((headers, bullets)); headers, bullets = [line.strip()], []
            else:
                # A new dated school following a completed degree is a new entry.
                previous_dates = any(RANGE.search(header) for header in headers)
                introduces_school = re.search(r'\b(university|college|institute|school|academy)\b', line, re.I)
                dated_title = RANGE.search(line) and RANGE.sub('', line).strip(' |,–—-')
                if section == 'education' and previous_dates and (introduces_school or dated_title):
                    blocks.append((headers, bullets)); headers, bullets = [], []
                headers.append(line.strip())
        if headers or bullets:
            blocks.append((headers, bullets))
        for index, (headers, bullets) in enumerate(blocks):
            if not headers:
                continue
            date = next((RANGE.search(h) for h in headers if RANGE.search(h)), None)
            clean = [RANGE.sub('', h).strip(' |,–—-') for h in headers]
            clean = [h for h in clean if h]
            ident = f'{section}-{index}'
            dates = {'start': date['start'] if date else '', 'end': date['end'] if date else ''}
            if section == 'education':
                profile[section].append({'org': clean[0] if clean else '', 'degree': clean[1] if len(clean) > 1 else '',
                                         'detail': ' | '.join(clean[2:]), **dates})
            elif section == 'experience':
                cells = [cell for header in clean for cell in columns(header)]
                role_index = next((i for i, cell in enumerate(cells) if ROLE.search(cell)), None)
                role = cells[role_index] if role_index is not None else (cells[1] if len(cells) > 1 else '')
                others = [cell for i, cell in enumerate(cells) if i != role_index] if role_index is not None else cells[:1] + cells[2:]
                employer_rows = [columns(header) for header in clean if role and role not in columns(header)]
                employer = next((row[0] for row in employer_rows if row and not re.fullmatch(r'remote|hybrid|on[- ]site|wfh', row[0], re.I)), others[0] if others else '')
                locations = others.copy()
                if employer in locations:
                    locations.remove(employer)
                profile[section].append({'id': ident, 'org': employer, 'role': role,
                                         'location': ' | '.join(locations), 'stack': [], 'max_bullets': 3, **dates,
                                         'bullets': [{'id': f'{ident}-{i}', 'text': b, 'status': 'shipped', 'angles': []} for i, b in enumerate(bullets)]})
            else:
                title = re.split(r'\s{2,}|\s*\|\s*', clean[0])[0] if clean else ''
                title = re.sub(r'\s*(?:GitHub|Repository|Source|Demo)?\s*\[https?://[^\]]*\]', '', title, flags=re.I).strip()
                project = {'id': ident, 'name': title, 'stack': [], 'angles': [],
                           'bullets': [{'id': f'{ident}-{i}', 'text': b, 'status': 'shipped', 'angles': []} for i, b in enumerate(bullets)]}
                project_links = web_links('\n'.join(headers))
                if project_links:
                    project['link'] = {'label': 'Project', 'url': project_links[0]}
                profile[section].append(project)
    for line in sections.get('skills', []):
        group, sep, values = line.strip().partition(':')
        if sep and group and not group.startswith('_'):
            profile['skills'][group] = [s.strip() for s in re.split(r'[,;]', values) if s.strip()]
    profile['coding_profiles'] = [{'text': BULLET.sub('', line).strip()} for line in sections.get('coding_profiles', [])]
    return profile


def review_summary(profile):
    """Prompts are suggestions, not invented defaults or mandatory social links."""
    missing = []
    for key in ('name', 'email', 'phone'):
        if not profile.get('basics', {}).get(key, '').strip():
            missing.append(f'Contact {key}: enter it if applicable.')
    links = web_links('', [link.get('url', '') for link in profile.get('basics', {}).get('links', [])])
    for name, host in (('GitHub', 'github.com'), ('LinkedIn', 'linkedin.com')):
        if not any((urlsplit(url).hostname or '').lower().removeprefix('www.') == host for url in links):
            missing.append(f'{name}: add your profile URL if you have one; otherwise leave it blank.')
    for section in ('experience', 'education', 'projects', 'skills'):
        if not profile.get(section):
            missing.append({'experience': 'Experience: add employment/internships, or confirm you are a fresher.',
                            'education': 'Education: add your institution, degree and dates.',
                            'projects': 'Projects: add your projects with at least three confirmed bullets each.',
                            'skills': 'Skills: add your confirmed technologies.'}[section])
    for section in ('experience', 'education'):
        for index, entry in enumerate(profile.get(section, [])):
            fields = ('org', 'role', 'start', 'end') if section == 'experience' else ('org', 'degree', 'start', 'end')
            absent = [field for field in fields if not entry.get(field, '').strip()]
            if absent:
                missing.append(f'{section.title()} {index + 1}: confirm ' + ', '.join(absent) + '.')
    for project in profile.get('projects', []):
        if len([b for b in project.get('bullets', []) if b.get('status', 'shipped') == 'shipped' and b.get('text', '').strip()]) < 3:
            missing.append(f'{project.get("name") or "Project"}: supply at least three confirmed bullets; missing claims are never generated.')
    from .jobs.eligibility import profile_years
    return {'missing': missing, 'experience_years': profile_years(profile)}

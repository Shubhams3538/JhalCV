"""Local profile/settings storage shared by the browser app and tests."""
from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from .tailor import ANGLE_KEYWORDS

ANGLES = set(ANGLE_KEYWORDS)

FROZEN = ['basics', 'experience', 'coding_profiles', 'education']


def text(value, label, limit=5000, required=False):
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        raise ValueError(f'{label}: enter text' + (f' (maximum {limit} characters).'))
    if any(ord(c) < 32 and c not in '\n\r\t' for c in value):
        raise ValueError(f'{label}: unsupported control character.')
    return value.strip()


def array(value, label, limit=100):
    if not isinstance(value, list) or len(value) > limit:
        raise ValueError(f'{label}: expected a list of at most {limit} entries.')
    return value


def obj(value, label):
    if not isinstance(value, dict):
        raise ValueError(f'{label}: expected an object.')
    return value


def link(value):
    obj(value, 'Link')
    text(value.get('label'), 'Link label', 80, True)
    url = text(value.get('url'), 'Link URL', 2000, True)
    parsed = urlsplit(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('Links must be full http:// or https:// URLs without credentials.')


def validate_profile(profile):
    obj(profile, 'Profile')
    basics = obj(profile.get('basics'), 'Contact details')
    for key in ('name', 'email', 'phone'):
        text(basics.get(key), key, 200, key == 'name')
    for item in array(basics.get('links', []), 'Contact links', 10):
        link(item)
    ids = set()
    for section in ('experience', 'projects', 'education', 'coding_profiles'):
        for entry in array(profile.get(section), section, 50):
            obj(entry, section)
            keys = {'experience': ('id', 'org', 'role', 'location', 'start', 'end'),
                    'projects': ('id', 'name'), 'education': ('org', 'degree', 'start', 'end', 'detail'),
                    'coding_profiles': ('text',)}[section]
            for key in keys:
                text(entry.get(key), f'{section}: {key}', 500, key in ('id', 'name', 'org', 'role', 'degree', 'text'))
            if 'link' in entry:
                link(entry['link'])
            for key in ('stack', 'angles'):
                for item in array(entry.get(key, []), key, 50):
                    text(item, key, 100, True)
                    if key == 'angles' and item not in ANGLES:
                        raise ValueError(f'Unknown angle: {item}')
            if section in ('experience', 'projects'):
                if entry['id'] in ids:
                    raise ValueError('Project and experience IDs must be unique.')
                ids.add(entry['id'])
                bullets = array(entry.get('bullets'), 'Bullets', 30)
                bullet_ids = set()
                for b in bullets:
                    obj(b, 'Bullet')
                    text(b.get('id'), 'Bullet ID', 100, True)
                    if b['id'] in bullet_ids:
                        raise ValueError('Bullet IDs must be unique within each entry.')
                    bullet_ids.add(b['id'])
                    text(b.get('text'), 'Bullet', 1200, True)
                    if b.get('status', 'shipped') not in ('planned', 'shipped'):
                        raise ValueError('Bullet status must be shipped or planned.')
                    for a in array(b.get('angles', []), 'Bullet angles', 10):
                        if a not in ANGLES:
                            raise ValueError(f'Unknown angle: {a}')
                if section == 'projects' and sum(b.get('status', 'shipped') == 'shipped' for b in bullets) < 2:
                    raise ValueError('Each project needs at least two confirmed, shipped bullets.')
                quota = entry.get('max_bullets', 4)
                if type(quota) is not int or not 1 <= quota <= 30:
                    raise ValueError('max_bullets must be between 1 and 30.')
            if section == 'projects':
                for angle, tagline in obj(entry.get('taglines', {}), 'Taglines').items():
                    if angle not in ANGLES:
                        raise ValueError(f'Unknown angle: {angle}')
                    text(tagline, 'Tagline', 300, True)
                if '_readme' in entry:
                    text(entry['_readme'], 'README', 30000)
    for label, items in obj(profile.get('skills'), 'Skills').items():
        if label.startswith('_'):
            continue
        text(label, 'Skill group', 100, True)
        for item in array(items, 'Skills', 100):
            text(item, 'Skill', 100, True)
    locked = obj(profile.get('_locked', {}), 'Locked settings')
    frozen = locked.get('frozen_sections', FROZEN)
    if not isinstance(frozen, list) or any(s not in FROZEN for s in frozen):
        raise ValueError('Invalid frozen sections.')
    cap = locked.get('max_projects_on_page', 2)
    if type(cap) is not int or not 1 <= cap <= 5:
        raise ValueError('Project limit must be between 1 and 5.')
    # The browser workflow always keeps these sections unchanged during tailoring.
    profile.setdefault('_locked', {})['frozen_sections'] = FROZEN[:]
    return profile


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name, suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            json.dump(value, out, indent=2, ensure_ascii=False)
            out.write('\n')
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class Store:
    def __init__(self, root):
        self.root = Path(root)
        self.profile_path = self.root / 'profile.json'
        self.settings_path = self.root / '.local' / 'settings.json'

    def profile(self):
        path = self.profile_path if self.profile_path.exists() else self.root / 'profile.example.json'
        return validate_profile(json.loads(path.read_text(encoding='utf-8')))

    def save_profile(self, profile):
        atomic_json(self.profile_path, validate_profile(profile))

    def settings(self):
        defaults = {'output_dir': str(self.root / 'build'), 'filename': 'shubham_Resume', 'tailorer': 'auto',
                    'model': 'gpt-5.5'}
        if self.settings_path.exists():
            defaults.update(json.loads(self.settings_path.read_text(encoding='utf-8')))
        return self.validate_settings(defaults)

    def validate_settings(self, value):
        obj(value, 'Settings')
        filename = text(value.get('filename'), 'Filename', 100, True)
        if not re.fullmatch(r'[\w -]+', filename) or filename in ('.', '..'):
            raise ValueError('Filename: use letters, numbers, spaces, hyphens or underscores; omit .pdf.')
        folder = text(value.get('output_dir'), 'Output folder', 2000, True)
        path = Path(folder).expanduser()
        if not path.is_absolute():
            path = self.root / path
        if value.get('tailorer') not in ('auto', 'rules'):
            raise ValueError('Choose auto or rules tailoring.')
        model = text(value.get('model'), 'Model', 100, True)
        return {'filename': filename, 'output_dir': str(path.resolve()), 'tailorer': value['tailorer'], 'model': model}

    def save_settings(self, value):
        settings = self.validate_settings(value)
        atomic_json(self.settings_path, settings)
        return settings

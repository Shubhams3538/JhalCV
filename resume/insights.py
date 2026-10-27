"""Skill insights across every job description the user tailors for.

Each job stores only the skills it asked for, keyed by a canonical name so that
"C++", "c++" and "cpp" are one skill. Whether a skill is missing is decided when
the insights are read, against the current profile, so adding a skill removes it
from the missing list immediately.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

from .storage import atomic_json

MAX_JOBS = 500
TOP = 10

# Spellings that mean the same skill. Keys and values are normalized forms.
CANONICAL = {
    'k8s': 'kubernetes', 'k8': 'kubernetes',
    'postgres': 'postgresql', 'postgre': 'postgresql', 'psql': 'postgresql',
    'golang': 'go', 'cpp': 'c++', 'c plus plus': 'c++',
    'node': 'node.js', 'nodejs': 'node.js', 'node js': 'node.js',
    'js': 'javascript', 'ts': 'typescript', 'py': 'python',
    'reactjs': 'react', 'react.js': 'react', 'react js': 'react',
    'nextjs': 'next.js', 'next': 'next.js', 'vuejs': 'vue', 'vue.js': 'vue',
    'expressjs': 'express', 'express.js': 'express',
    'mongo': 'mongodb', 'sklearn': 'scikit-learn', 'tf': 'tensorflow',
    'dotnet': '.net', 'amazon web services': 'aws', 'google cloud': 'gcp',
    'restful': 'rest', 'rest api': 'rest', 'rest apis': 'rest', 'restful apis': 'rest',
    'ci cd': 'ci/cd', 'cicd': 'ci/cd', 'ml': 'machine learning',
    'llm': 'llms', 'gen ai': 'genai', 'generative ai': 'genai',
}

# How a canonical skill is shown when the profile does not spell it already.
DISPLAY = {
    'c++': 'C++', 'c#': 'C#', 'go': 'Go', 'python': 'Python', 'java': 'Java', 'rust': 'Rust',
    'javascript': 'JavaScript', 'typescript': 'TypeScript', 'node.js': 'Node.js',
    'react': 'React', 'next.js': 'Next.js', 'vue': 'Vue', 'kubernetes': 'Kubernetes',
    'postgresql': 'PostgreSQL', 'mongodb': 'MongoDB', 'mysql': 'MySQL', 'sqlite': 'SQLite',
    'graphql': 'GraphQL', 'grpc': 'gRPC', 'rest': 'REST', 'fastapi': 'FastAPI',
    'github': 'GitHub', 'gitlab': 'GitLab', 'openai': 'OpenAI', 'llms': 'LLMs',
    'genai': 'GenAI', 'pytorch': 'PyTorch', 'tensorflow': 'TensorFlow', 'numpy': 'NumPy',
    'ci/cd': 'CI/CD', '.net': '.NET', 'dynamodb': 'DynamoDB', 'langchain': 'LangChain',
    'langgraph': 'LangGraph', 'llamaindex': 'LlamaIndex', 'huggingface': 'Hugging Face',
    'rabbitmq': 'RabbitMQ', 'elasticsearch': 'Elasticsearch', 'opentelemetry': 'OpenTelemetry',
    'machine learning': 'Machine learning', 'scikit-learn': 'scikit-learn', 'pgvector': 'pgvector',
}


def canonical(skill) -> str:
    key = re.sub(r'\s+', ' ', str(skill).strip().lower()).strip(' ,;:')
    key = re.sub(r'(?<=\w)[-_](?=\w)', ' ', key) if key not in DISPLAY and key not in CANONICAL else key
    return CANONICAL.get(key, key)


def display(key: str, seen: str = '') -> str:
    if key in DISPLAY:
        return DISPLAY[key]
    if seen and seen != seen.lower():
        return seen
    return key.upper() if len(key) <= 3 else key[:1].upper() + key[1:]


def profile_skills(profile: dict) -> dict:
    """Every skill the profile claims anywhere, canonical key -> profile spelling."""
    names = [i for k, v in profile.get('skills', {}).items() if not k.startswith('_') and isinstance(v, list) for i in v]
    names += [s for e in profile.get('experience', []) for s in e.get('stack', [])]
    names += [s for p in profile.get('projects', []) for s in p.get('stack', [])]
    owned = {}
    for name in names:
        owned.setdefault(canonical(name), name)
    return owned


def job_id(jd: str) -> str:
    return hashlib.sha256(re.sub(r'\s+', ' ', jd.strip().lower()).encode()).hexdigest()


class Insights:
    def __init__(self, root):
        self.path = Path(root) / '.local' / 'insights.json'

    def load(self) -> dict:
        if not self.path.exists():
            return {'jobs': [], 'hidden': []}
        data = json.loads(self.path.read_text(encoding='utf-8'))
        return {'jobs': data.get('jobs', []), 'hidden': data.get('hidden', [])}

    def record(self, jd: str, skills) -> None:
        """Store what one job asked for. The same JD counts once; the latest analysis wins."""
        asked = {}
        for skill in skills:
            key = canonical(skill)
            if key and len(key) <= 60:
                asked.setdefault(key, display(key, str(skill).strip()))
        data = self.load()
        ident = job_id(jd)
        data['jobs'] = [j for j in data['jobs'] if j.get('id') != ident]
        data['jobs'].append({'id': ident, 'at': int(time.time()), 'asked': asked})
        data['jobs'] = data['jobs'][-MAX_JOBS:]
        atomic_json(self.path, data)

    def hide(self, skill) -> None:
        data = self.load()
        key = canonical(skill)
        if key and key not in data['hidden']:
            data['hidden'].append(key)
            atomic_json(self.path, data)

    def unhide_all(self) -> None:
        data = self.load()
        data['hidden'] = []
        atomic_json(self.path, data)

    def reset(self) -> None:
        atomic_json(self.path, {'jobs': [], 'hidden': []})

    def summary(self, profile: dict) -> dict:
        data = self.load()
        owned = profile_skills(profile)
        hidden = set(data['hidden'])
        counts, names = {}, {}
        for job in data['jobs']:
            for key, name in job.get('asked', {}).items():
                counts[key] = counts.get(key, 0) + 1
                names.setdefault(key, name)
        total = len(data['jobs'])

        def row(key):
            return {'key': key, 'skill': owned.get(key) or names[key], 'count': counts[key],
                    'percent': round(100 * counts[key] / total) if total else 0, 'owned': key in owned}

        ranked = sorted(counts, key=lambda k: (-counts[k], names[k].lower()))
        missing = [k for k in ranked if k not in owned and k not in hidden]
        return {'jobs': total, 'hidden': len(hidden),
                'missing': [row(k) for k in missing[:TOP]],
                'asked': [row(k) for k in ranked[:TOP]]}


def add_skill(profile: dict, skill, group) -> dict:
    """Add a skill to an existing group, refusing anything the profile already lists."""
    name = str(skill or '').strip()
    if not name or len(name) > 100:
        raise ValueError('Choose a skill to add.')
    groups = [k for k, v in profile.get('skills', {}).items() if not k.startswith('_') and isinstance(v, list)]
    if group not in groups:
        raise ValueError('Choose one of your skill groups.')
    key = canonical(name)
    for label in groups:
        if any(canonical(item) == key for item in profile['skills'][label]):
            raise ValueError(f'{name} is already in your {label} skills.')
    profile['skills'][group].append(display(key, name))
    return profile

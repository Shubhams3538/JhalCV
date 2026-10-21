"""Render a reviewed draft at the original layout, then export without overwrites."""
from __future__ import annotations

import copy
import os
import secrets
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .descriptions import within
from .render import fit, compile_pdf, page_count, RenderError
from .storage import atomic_json
from .verify import verify


def build_preview(draft, edits, root, ident):
    root = Path(root).resolve()
    source = copy.deepcopy(draft['source'])
    if not isinstance(edits, list) or len(edits) != len(source['projects']):
        raise ValueError('Project list changed. Create a new tailored draft.')
    proposed = copy.deepcopy(source)
    for old, new, edit in zip(source['projects'], proposed['projects'], edits):
        bullets = edit.get('bullets') if isinstance(edit, dict) else None
        if not isinstance(bullets, list) or len(bullets) != len(old['bullets']):
            raise ValueError('Keep the original bullet count.')
        for i, (original, revised) in enumerate(zip(old['bullets'], bullets)):
            if not within(revised, original):
                raise ValueError(f'{old["name"]}, bullet {i+1}: keep within the original character/word budget and do not add numbers.')
        new['bullets'] = [b.strip() for b in bullets]
    directory = root / '.cache' / 'web' / ident / secrets.token_hex(8)
    directory.mkdir(parents=True, exist_ok=True)
    template = root / 'templates' / 'classic.typ'
    baseline_pdf = directory / 'baseline.pdf'
    result = fit(source, baseline_pdf, root=root, template=template, data_path=directory / 'baseline.data.json', max_pages=1)
    if result['pages'] != 1:
        raise ValueError('Your original content cannot fit on one page. Shorten your saved profile, especially the fixed sections, before exporting.')
    notices = []
    if result['dropped']:
        notices.append(f'The baseline fitter omitted {len(result["dropped"])} lower-ranked project bullet(s) to keep one page.')
    # Use exactly the baseline typography. Do not squeeze AI wording into a new preset.
    candidate = copy.deepcopy(source)
    for original, rewritten, target in zip(draft['source']['projects'], proposed['projects'], candidate['projects']):
        replacements = {m['id']: b for m, b in zip(original['_bullet_meta'], rewritten['bullets'])}
        target['bullets'] = [replacements[m['id']] for m in target['_bullet_meta']]
    out = directory / 'resume.pdf'
    compile_pdf(candidate, out, root=root, template=template, data_path=directory / 'resume.data.json')
    if page_count(out) != 1:
        candidate = source
        out.write_bytes(baseline_pdf.read_bytes())
        atomic_json(directory / 'resume.data.json', source)
        notices.append('Reviewed wording wrapped beyond one page. Restored original project wording at the same layout; please review this PDF before saving.')
    checks = verify(out, candidate, max_pages=1)
    blocking = [c for c in checks if c['check'] not in ('glued-words', 'columns')]
    if blocking:
        raise ValueError('PDF verification failed: ' + '; '.join(c['check'] + ': ' + c['detail'] for c in blocking))
    revision = secrets.token_urlsafe(18)
    atomic_json(directory / 'resume.report.json', {
        'generated': datetime.now(timezone.utc).isoformat(), 'jd_text': draft['jd'], 'report': draft['report'],
        'wording_notice': draft['notice'], 'fit': result, 'layout_notices': notices, 'verify_failures': checks,
    })
    draft['preview'] = {'revision': revision, 'path': out, 'checks': checks, 'notices': notices}
    return {'revision': revision, 'url': f'/api/pdf/{ident}/{revision}', 'checks': checks, 'notices': notices, 'pages': 1}


def export_preview(draft, revision, settings):
    preview = draft.get('preview')
    if not preview or not isinstance(revision, str) or revision != preview['revision']:
        raise ValueError('Build and review the latest PDF preview before saving.')
    if preview.get('exported'):
        return {'path': str(preview['exported'])}
    destination = Path(settings['output_dir'])
    destination.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=destination, prefix='.jhalcv-', suffix='.tmp')
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(preview['path'].read_bytes())
            out.flush()
            os.fsync(out.fileno())
        # Hard-link a fully written temporary file: atomic creation without ever
        # replacing an existing resume or following a destination symlink.
        for number in range(1, 10001):
            suffix = '' if number == 1 else f'_{number}'
            target = destination / f'{settings["filename"]}{suffix}.pdf'
            try:
                os.link(temporary, target)
                preview['exported'] = target
                return {'path': str(target)}
            except FileExistsError:
                continue
        raise ValueError('Too many files with this name. Choose another filename in Settings.')
    finally:
        Path(temporary).unlink(missing_ok=True)

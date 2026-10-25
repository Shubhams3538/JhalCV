"""Render a reviewed draft; keep one current PDF and archive previous versions."""
from __future__ import annotations

import copy
import shutil
from contextlib import contextmanager
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



@contextmanager
def folder_lock(destination):
    # Keep a stable lock file: removing it could let different processes lock
    # different inodes. OS locks are released automatically on process exit.
    flags = os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(destination / '.jhalcv-save.lock', flags, 0o600)
    with os.fdopen(fd, 'r+b') as lock:
        if os.name == 'nt':
            import msvcrt
            if os.fstat(lock.fileno()).st_size == 0:
                lock.write(b'0'); lock.flush()
            lock.seek(0)
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as e:
                raise ValueError('Another resume is being saved in this folder. Try again in a moment.') from e
        else:
            import fcntl
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as e:
                raise ValueError('Another resume is being saved in this folder. Try again in a moment.') from e
        try:
            yield
        finally:
            if os.name == 'nt':
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock, fcntl.LOCK_UN)

def export_preview(draft, revision, settings):
    preview = draft.get('preview')
    if not preview or not isinstance(revision, str) or revision != preview['revision']:
        raise ValueError('Build and review the latest PDF preview before saving.')
    destination = Path(settings['output_dir'])
    destination.mkdir(parents=True, exist_ok=True)
    with folder_lock(destination):
        return _save_current(preview, destination, settings['filename'])


def _save_current(preview, destination, filename):
    target = destination / f'{filename}.pdf'
    # Re-saving the same preview is a no-op only while that exact PDF is current.
    # If another draft replaced it, explicitly saving this preview restores it.
    if preview.get('exported') == target and target.is_file() and not target.is_symlink():
        if target.read_bytes() == preview['path'].read_bytes():
            return {'path': str(target)}
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise ValueError('The resume destination must be a regular PDF file, not a link or folder.')
    fd, temporary = tempfile.mkstemp(dir=destination, prefix='.jhalcv-', suffix='.tmp')
    archived = None
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(preview['path'].read_bytes())
            out.flush()
            os.fsync(out.fileno())
        if target.exists():
            archive = destination / 'Archive'
            if archive.is_symlink():
                raise ValueError('Archive must be a regular folder, not a symbolic link.')
            archive.mkdir(exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime('%Y-%m-%d_%H-%M-%S_%f')
            for _ in range(10):
                archived = archive / f'{filename}_{stamp}_{secrets.token_hex(4)}.pdf'
                created = False
                try:
                    # Exclusive copying also works on drives without hard links.
                    # Keep the original timestamps on archived versions.
                    with archived.open('xb') as backup:
                        created = True
                        with target.open('rb') as previous:
                            shutil.copyfileobj(previous, backup)
                        backup.flush()
                        os.fsync(backup.fileno())
                    shutil.copystat(target, archived, follow_symlinks=False)
                    break
                except FileExistsError:
                    if created:
                        archived.unlink(missing_ok=True)
                        raise
                    archived = None
                except OSError:
                    if created:
                        archived.unlink(missing_ok=True)
                    raise
            if archived is None:
                raise ValueError('Could not reserve an archive filename. Try saving again.')
        # The complete new PDF becomes current atomically; failures above leave
        # the previous current PDF intact. Archive names are never overwritten.
        os.replace(temporary, target)
        preview['exported'] = target
        return {'path': str(target), **({'archived': str(archived)} if archived else {})}
    finally:
        Path(temporary).unlink(missing_ok=True)

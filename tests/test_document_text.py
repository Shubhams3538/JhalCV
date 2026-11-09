import base64
import io
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import zipfile

from resume.document_text import pdf_xml_text
from resume.onboarding import extract, empty_profile
from resume.resume_import import suggested_profile


class DocumentTextTests(unittest.TestCase):
    def test_role_first_experience_maps_to_role_not_company(self):
        profile = suggested_profile('Jane Doe\nEXPERIENCE\nSoftware Engineer    Jan 2023 - Jan 2024\nExample Inc    Remote\n• Built an API.', empty_profile)
        entry = profile['experience'][0]
        self.assertEqual((entry['role'], entry['org'], entry['location']), ('Software Engineer', 'Example Inc', 'Remote'))
        profile = suggested_profile('Jane Doe\nEXPERIENCE\nSoftware Engineer    Remote    Jan 2023 - Jan 2024\nExample Inc\n• Built an API.', empty_profile)
        entry = profile['experience'][0]
        self.assertEqual((entry['role'], entry['org'], entry['location']), ('Software Engineer', 'Example Inc', 'Remote'))

    def test_docx_same_github_label_retains_each_project_target(self):
        ns = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
        rel = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
        def p(text): return f'<w:p><w:r><w:t>{text}</w:t></w:r></w:p>'
        def project(name, ident): return f'<w:p><w:r><w:t>{name} | </w:t></w:r><w:hyperlink r:id="{ident}"><w:r><w:t>GitHub</w:t></w:r></w:hyperlink></w:p>' + ''.join(p('• ' + bullet) for bullet in ('Built endpoints.', 'Added tests.', 'Added tracing.'))
        xml = f'<w:document xmlns:w="{ns}" xmlns:r="{rel}"><w:body>' + p('Jane Doe') + p('jane@example.com') + p('PROJECTS') + project('Task API', 'one') + project('Search API', 'two') + '</w:body></w:document>'
        links = '<Relationships>' + ''.join(f'<Relationship Id="{ident}" Type="{rel}/hyperlink" Target="https://github.com/jane/{name}" TargetMode="External"/>' for ident, name in (('one', 'tasks'), ('two', 'search'))) + '</Relationships>'
        out = io.BytesIO()
        with zipfile.ZipFile(out, 'w') as archive:
            archive.writestr('word/document.xml', xml)
            archive.writestr('word/_rels/document.xml.rels', links)
        result = extract({'name': 'resume.docx', 'data': base64.b64encode(out.getvalue()).decode()})
        self.assertEqual([(p['name'], p['link']['url']) for p in result['profile']['projects']], [('Task API', 'https://github.com/jane/tasks'), ('Search API', 'https://github.com/jane/search')])
        self.assertIn('Task API | GitHub [https://github.com/jane/tasks]', result['text'])

    def test_pdf_xml_preserves_link_labels_and_rows(self):
        xml = '<pdf2xml><page><text top="10" left="20">Task API</text><text top="11" left="200"><a href="https://github.com/jane/tasks">GitHub</a></text><text top="30" left="20">Built endpoints.</text></page></pdf2xml>'
        value = pdf_xml_text(xml)
        self.assertIn('Task API    GitHub [https://github.com/jane/tasks]', value)
        self.assertEqual(value.count('https://github.com/jane/tasks'), 1)
        value = pdf_xml_text('<pdf2xml><page><text top="10" left="20"><b>Software</b> <i>Engineer</i></text></page></pdf2xml>')
        self.assertEqual(value, 'Software Engineer')
        value = pdf_xml_text('<pdf2xml><page><text top="10" left="20">PROJECTS</text><text top="30" left="20">Task API</text><text top="50" left="20">• Built endpoints with</text><text top="70" left="30">retries and detailed tracing.</text><text top="90" left="20">• Added tests.</text><text top="110" left="20">• Added metrics.</text></page></pdf2xml>')
        profile = suggested_profile('Jane Doe\n' + value, empty_profile)
        self.assertEqual(len(profile['projects']), 1)
        self.assertEqual(profile['projects'][0]['bullets'][0]['text'], 'Built endpoints with retries and detailed tracing.')

    @unittest.skipUnless(all(shutil.which(cmd) for cmd in ('typst', 'pdftotext', 'pdftohtml')), 'PDF tools required')
    def test_real_pdf_hidden_project_hyperlinks_and_role_first_fields(self):
        source = '''#set page(width: 210mm, height: 297mm)
#set text(size: 11pt)
Jane Doe\\
jane\@example.com\\
#link("https://github.com/jane")[GitHub] #link("https://linkedin.com/in/jane")[LinkedIn]
= EXPERIENCE
Software Engineer #h(1fr) Jan 2023 - Jan 2024\\
Example Inc #h(1fr) Remote
- Built an API.
- Added tests.
- Added tracing.
= PROJECTS
Task API #h(1fr) #link("https://github.com/jane/tasks")[GitHub]
- Built endpoints with resilient request processing, graceful errors, explicit validation, retries and detailed tracing across every service and database operation for users.
- Added tests.
- Added tracing.
Search API #h(1fr) #link("https://github.com/jane/search")[GitHub]
- Built search.
- Added indexes.
- Added query tests.
'''
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'resume.typ').write_text(source)
            subprocess.run(['typst', 'compile', str(root / 'resume.typ'), str(root / 'resume.pdf')], check=True, capture_output=True)
            result = extract({'name': 'resume.pdf', 'data': base64.b64encode((root / 'resume.pdf').read_bytes()).decode()})
        self.assertEqual(result['profile']['experience'][0]['role'], 'Software Engineer')
        self.assertEqual(result['profile']['experience'][0]['org'], 'Example Inc')
        self.assertEqual([(p['name'], p.get('link', {}).get('url')) for p in result['profile']['projects']], [('Task API', 'https://github.com/jane/tasks'), ('Search API', 'https://github.com/jane/search')])
        self.assertIn('database operation for users.', result['profile']['projects'][0]['bullets'][0]['text'])
        self.assertEqual([link['label'] for link in result['profile']['basics']['links']], ['GitHub', 'LinkedIn'])


if __name__ == '__main__': unittest.main()

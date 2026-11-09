import base64
import io
import unittest
import zipfile
from unittest.mock import patch

from resume.onboarding import empty_profile, extract, structure
from resume.resume_import import suggested_profile, review_summary

RESUME = '''Jane Doe
jane@example.com | +91 9876543210
https://github.com/janedoe | https://www.linkedin.com/in/jane-doe
EXPERIENCE
Example Inc       Jan 2023 - Jan 2024
Software Engineer
• Built an API in Python.
• Added database tests.
• Improved request tracing.
EDUCATION
Example University       2018 - 2022
B.Tech in Computer Science
CGPA: 8.5
PROJECTS
Task API | https://github.com/janedoe/task-api
• Built authenticated endpoints.
• Added database migrations.
• Tested failed requests.
TECHNICAL SKILLS
Languages: Python, Go
Backend: FastAPI, PostgreSQL
'''


class ResumeImportTests(unittest.TestCase):
    def test_education_dates_on_their_own_line_stay_with_the_school(self):
        profile = suggested_profile('Jane Doe\nEDUCATION\nExample University\nB.Tech Computer Science\n2018 - 2022\nSecond College\nM.Tech\n2022 - 2024', empty_profile)
        self.assertEqual(len(profile['education']), 2)
        first, second = profile['education']
        self.assertEqual((first['org'], first['degree'], first['start'], first['end']), ('Example University', 'B.Tech Computer Science', '2018', '2022'))
        self.assertEqual((second['org'], second['degree'], second['start'], second['end']), ('Second College', 'M.Tech', '2022', '2024'))

    def test_indented_employer_is_never_appended_to_another_job_bullet(self):
        profile = suggested_profile('Jane Doe\nEXPERIENCE\nFirst Inc 2020 - 2021\nEngineer\n  • Built API.\n  Second Inc\n  Engineer 2022 - 2023\n  • Built UI.', empty_profile)
        self.assertEqual(len(profile['experience']), 2)
        self.assertEqual(profile['experience'][0]['bullets'][0]['text'], 'Built API.')
        self.assertEqual(profile['experience'][1]['org'], 'Second Inc')
        self.assertEqual(profile['experience'][1]['role'], 'Engineer')

    def test_local_draft_populates_sections_links_and_suggests_experience(self):
        profile = suggested_profile(RESUME, empty_profile)
        self.assertEqual(profile['basics']['name'], 'Jane Doe')
        self.assertEqual(profile['basics']['phone'], '+91 9876543210')
        self.assertEqual([link['label'] for link in profile['basics']['links']], ['GitHub', 'LinkedIn'])
        self.assertEqual(profile['experience'][0]['org'], 'Example Inc')
        self.assertEqual(profile['experience'][0]['role'], 'Software Engineer')
        self.assertEqual(profile['experience'][0]['start'], 'Jan 2023')
        self.assertEqual(profile['experience'][0]['end'], 'Jan 2024')
        self.assertEqual(len(profile['experience'][0]['bullets']), 3)
        self.assertEqual(profile['education'][0]['org'], 'Example University')
        self.assertEqual(profile['education'][0]['degree'], 'B.Tech in Computer Science')
        self.assertEqual(profile['projects'][0]['link']['url'], 'https://github.com/janedoe/task-api')
        self.assertEqual(profile['skills']['Languages'], ['Python', 'Go'])
        self.assertEqual(review_summary(profile)['experience_years'], 1.1)

    def test_missing_fields_are_prompted_without_inventing_experience(self):
        profile = suggested_profile('Jane Doe\njane@example.com\nEDUCATION\nExample University', empty_profile)
        self.assertEqual(profile['experience'], [])
        self.assertEqual(profile['education'][0]['degree'], '')
        self.assertEqual(profile['education'][0]['start'], '')
        prompts = ' '.join(review_summary(profile)['missing'])
        for word in ('GitHub', 'LinkedIn', 'fresher', 'degree', 'Projects'):
            self.assertIn(word, prompts)
        self.assertNotIn('phone', profile['basics']['phone'])
        # A year range is not a telephone number.
        profile = suggested_profile('Jane Doe\nEducation\nExample University 2018 - 2022\nB.Tech', empty_profile)
        self.assertEqual(profile['basics']['phone'], '')

    def test_docx_hyperlink_targets_populate_even_when_labels_hide_urls(self):
        out = io.BytesIO()
        with zipfile.ZipFile(out, 'w') as archive:
            archive.writestr('word/document.xml', '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Jane Doe</w:t></w:r></w:p><w:p><w:r><w:t>jane@example.com | GitHub | LinkedIn</w:t></w:r></w:p></w:body></w:document>')
            archive.writestr('word/_rels/document.xml.rels', '<Relationships><Relationship Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" Target="https://github.com/janedoe" TargetMode="External"/><Relationship Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" Target="https://linkedin.com/in/jane" TargetMode="External"/><Relationship Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" Target="file:///private/secret" TargetMode="External"/></Relationships>')
        result = extract({'name': 'resume.docx', 'data': base64.b64encode(out.getvalue()).decode()})
        self.assertEqual(len(result['profile']['basics']['links']), 2)
        self.assertIn('https://github.com/janedoe', result['text'])
        self.assertNotIn('file:///private/secret', result['text'])

    def test_ai_draft_keeps_original_bullets_and_locally_found_profile_links(self):
        raw = {'name': 'Jane Doe', 'email': 'jane@example.com', 'phone': '', 'links': [],
               'experience': [{'org': 'Example Inc', 'role': 'Software Engineer', 'location': '', 'start': 'Jan 2023', 'end': 'Jan 2024', 'bullets': ['Built an API in Python.']}],
               'education': [{'org': 'Example University', 'degree': 'B.Tech', 'start': '2018', 'end': '2022', 'detail': ''}],
               'coding_profiles': [], 'skills': [{'group': 'Languages', 'items': ['Python']}], 'projects': []}
        with patch('resume.onboarding.scope'), patch('resume.onboarding.llm.ask', return_value=(raw, False)) as ask:
            profile = structure(RESUME, '.', None, 'codex')
        self.assertEqual(ask.call_count, 1)
        self.assertEqual(profile['experience'][0]['bullets'][0]['text'], 'Built an API in Python.')
        self.assertEqual(len(profile['basics']['links']), 2)
        self.assertEqual(profile['basics']['phone'], '+91 9876543210')


if __name__ == '__main__': unittest.main()

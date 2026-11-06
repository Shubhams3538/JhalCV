"""Verify the job-to-draft flow without opening browsers or calling models."""
import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which('node'), 'Node is required for frontend checks')
class DraftFlowUiTests(unittest.TestCase):
    def test_job_tailor_creates_draft_directly_from_full_library(self):
        script = r"""
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const source = fs.readFileSync(process.argv[1], 'utf8'), nodes = new Map(), calls = [];
const $ = selector => {
  if (!nodes.has(selector)) nodes.set(selector, {value: '', open: false, scrollTop: 0, dispatchEvent() {}});
  return nodes.get(selector);
};
let description = 'Build reliable Python backend services and REST APIs with database integrations.';
const context = { $, state: {example: false}, draft: null, jobsTab: 'eligible',
  window: {scrollY: 100, scrollTo() {}}, Event: class {},
  api: async (path, body) => {calls.push({path, body});
    if (path === 'jobs/detail') return {title: 'Engineer', company: 'Acme', location: 'India', description};
    if (path === 'jobs/status') return {};
    if (path === 'tailor') return {id: 'new-draft'};
    throw new Error('Unexpected request');
  },
  renderJobs() {}, saveTailoringJob() {}, showView(view) {calls.push({view})},
  saveProfile: async () => {calls.push({save: true})},
  startTailoringProgress() {}, stopTailoringProgress() {},
  renderDraft() {calls.push({render: true})}, notify() {}};
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('async function tailorForJob('), source.indexOf('function insightBar(')), context);
vm.runInContext(source.slice(source.indexOf('async function createDraft('), source.indexOf('function renderDraft(')), context);
(async () => {
  const job = {id: 1, title: 'Engineer', company: 'Acme', url: 'https://example.com/job', status: 'new'};
  await context.tailorForJob(job);
  assert.equal(calls.filter(c => c.path === 'tailor').length, 1);
  const request = calls.find(c => c.path === 'tailor');
  assert.deepStrictEqual(Object.keys(request.body), ['jd']);
  assert(request.body.jd.includes(description));
  assert.equal(context.draft.id, 'new-draft');
  assert.equal(context.tailoringJob.id, job.id);
  assert(calls.findIndex(c => c.view === 'workspace') < calls.indexOf(request));
  assert(calls.some(c => c.render));
  description = '';
  await assert.rejects(() => context.tailorForJob(job), /no description/);
  assert.equal(calls.filter(c => c.path === 'tailor').length, 1);
})().catch(e => {console.error(e); process.exitCode = 1});
"""
        source = Path(__file__).resolve().parents[1] / 'web' / 'app.js'
        result = subprocess.run(['node', '-e', script, str(source)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

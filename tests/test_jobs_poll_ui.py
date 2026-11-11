"""Execute polling and rendering behavior without a browser or live services."""
import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which('node'), 'Node is required for frontend checks')
class JobsPollUiTests(unittest.TestCase):
    def test_progress_polling_retries_and_ignores_stale_views(self):
        script = r"""
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const source = fs.readFileSync(process.argv[1], 'utf8');
const progress = {running: true, completed: 1, review_completed: 0};
const calls = [], timers = [];
const ctx = {viewRevision: 1, jobsData: {progress: {...progress}}, jobsFullPollAt: Date.now(),
  jobsPoll: null, document: {hidden: false}, Date, JSON,
  clearTimeout() {}, setTimeout(fn, ms) {timers.push(fn); return 1},
  updateActivity() {}, renderJobs(data) {calls.push('render'); ctx.jobsData = data},
  api: async path => {calls.push(path); return path === 'jobs/progress' ? {progress: {...progress}} : {progress: {...progress}}}
};
vm.createContext(ctx);
vm.runInContext(source.slice(source.indexOf('function jobsProgressKey('), source.indexOf('let companyImport')), ctx);
(async () => {
  await ctx.pollJobs();
  assert.deepStrictEqual(calls, ['jobs/progress']);
  assert.equal(timers.length, 1);
  calls.length = 0; progress.completed++;
  await ctx.pollJobs();
  assert.deepStrictEqual(calls, ['jobs/progress', 'jobs', 'render']);
  const before = timers.length;
  ctx.api = async () => {throw new Error('offline')};
  await ctx.pollJobs();
  assert.equal(timers.length, before + 1);
  calls.length = 0;
  ctx.api = async () => {ctx.viewRevision++; return {progress}};
  await ctx.pollJobs();
  assert.deepStrictEqual(calls, []);
  assert.equal(timers.length, before + 1);
})().catch(e => {console.error(e); process.exitCode = 1});
"""
        self.run_script(script)

    def test_unchanged_rows_survive_progress_only_renders(self):
        script = r"""
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const source = fs.readFileSync(process.argv[1], 'utf8'), nodes = new Map();
const $ = selector => {
  if (!nodes.has(selector)) nodes.set(selector, {value: '', children: [],
    append(...items) {this.children.push(...items)}, replaceChildren() {this.children = []},
    removeChild(child) {this.children.splice(this.children.indexOf(child), 1)},
    insertBefore(child, before) {
      const old = this.children.indexOf(child); if (old >= 0) this.children.splice(old, 1);
      this.children.splice(before ? this.children.indexOf(before) : this.children.length, 0, child);
    }});
  return nodes.get(selector);
};
const ctx = {$, jobsTab: 'eligible', jobsData: {jobs: [{id: 1, category: 'eligible', title: 'Engineer',
  company: 'Acme', location: 'India', eligible: 'yes', relevant: 'yes', reason: 'Likely match'}]},
  document: {querySelectorAll() {return []}}, jobInTab: (j, group) => j.category === group,
  el: (tag, props, children = []) => ({tag, props, children, dataset: {jobId: props['data-job-id']}, classList: {remove() {}},
    append(...items) {this.children.push(...items)}})};
vm.createContext(ctx);
vm.runInContext(source.slice(source.indexOf('function renderJobList('), source.indexOf('let tailoringJob')), ctx);
ctx.renderJobList();
const first = $('#jobs-list').children[0];
ctx.renderJobList();
assert.strictEqual($('#jobs-list').children[0], first);
ctx.jobsData.jobs[0].reason = 'Updated requirement';
ctx.renderJobList();
assert.notStrictEqual($('#jobs-list').children[0], first);
const stable = $('#jobs-list').children[0];
ctx.jobsData.jobs.push({...ctx.jobsData.jobs[0], id: 2, title: 'Another engineer'});
ctx.renderJobList();
assert.strictEqual($('#jobs-list').children[0], stable);
const second = $('#jobs-list').children[1];
ctx.jobsData.jobs[1].reason = 'Changed only second job';
ctx.renderJobList();
assert.strictEqual($('#jobs-list').children[0], stable);
assert.notStrictEqual($('#jobs-list').children[1], second);
ctx.jobsData.jobs.reverse();
ctx.renderJobList();
assert.strictEqual($('#jobs-list').children[1], stable);
ctx.jobsData.jobs.shift();
ctx.renderJobList();
assert.strictEqual($('#jobs-list').children.length, 1);
assert.strictEqual($('#jobs-list').children[0], stable);
ctx.jobsData.jobs = [];
ctx.renderJobList();
assert.equal($('#jobs-list').children[0].props.text, 'Jobs appear here after a refresh.');
ctx.jobsData.jobs = [{id: 2, category: 'no'}];
ctx.renderJobList();
assert.equal($('#jobs-list').children[0].props.text, 'No jobs in this section.');
ctx.jobsData.jobs = Array.from({length: 1000}, (_, i) => ({id: i, category: 'eligible',
  title: 'Engineer ' + i, company: 'Acme', location: 'India', eligible: 'yes', relevant: 'yes', reason: 'Match'}));
ctx.renderJobList();
const thousand = [...$('#jobs-list').children];
let created = 0;
const originalEl = ctx.el;
ctx.el = (...args) => {created++; return originalEl(...args)};
ctx.jobsData.jobs[500].reason = 'Updated assessment';
ctx.renderJobList();
assert(created < 30, 'One update rebuilt the large list');
thousand.forEach((row, index) => {
  if (index === 500) assert.notStrictEqual($('#jobs-list').children[index], row);
  else assert.strictEqual($('#jobs-list').children[index], row);
});
"""
        self.run_script(script)

    def run_script(self, script):
        source = Path(__file__).resolve().parents[1] / 'web' / 'app.js'
        result = subprocess.run(['node', '-e', script, str(source)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

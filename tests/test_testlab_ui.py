"""Exercise Test lab polling without a browser or real inference."""
import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which('node'), 'Node is required for frontend checks')
class TestLabUiTests(unittest.TestCase):
    def test_completion_errors_and_poll_recovery(self):
        script = r"""
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const source = fs.readFileSync(process.argv[1], 'utf8');
const nodes = new Map();
function node() { return {children: [], hidden: true, disabled: false,
  append(...v) {this.children.push(...v)}, prepend(...v) {this.children.unshift(...v)},
  replaceChildren(...v) {this.children = v}}; }
const timers = [], context = {viewRevision: 0, testLabPoll: null,
  TEST_CATEGORY_LABELS: {}, clearTimeout() {},
  setTimeout(fn, delay) {timers.push(delay); return timers.length},
  $(selector) {if (!nodes.has(selector)) nodes.set(selector, node()); return nodes.get(selector)},
  el(tag, props, children = []) {return {...node(), ...props, children}},
  api: async () => ({jobs: [], progress: {running: false, error: 'Review failed'}})};
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('function renderTestLab('),
  source.indexOf('async function pollActivity()')), context);
(async () => {
  const failed = {running: false, finished_at: 20, error: 'Review failed'};
  assert.equal(context.activityProgress({progress: {running: false}, test_progress: failed}), failed);
  const newer = {running: false, finished_at: 30};
  assert.equal(context.activityProgress({progress: newer, test_progress: failed}), newer);
  context.activityProgress({test_progress: {running: true}});
  assert.equal(context.activityProgress({progress: {running: false, finished_at: 20}, test_progress: failed}), failed);
  await context.updateTestLab();
  assert.equal(nodes.get('#testlab-results').children[0].text, 'Test failed: Review failed');
  assert.equal(nodes.get('#testlab-run').disabled, false);
  context.api = async () => {throw new Error('Temporary network failure')};
  await context.updateTestLab();
  assert.equal(timers.at(-1), 3000);
  context.api = async () => ({jobs: [], progress: {running: false}});
  await context.updateTestLab();
  assert.equal(nodes.get('#testlab-run').disabled, false);
  assert.equal(nodes.get('#testlab-results').children[0].text, 'No test jobs collected yet.');
})().catch(e => {console.error(e); process.exitCode = 1});
"""
        source = Path(__file__).resolve().parents[1] / 'web' / 'app.js'
        result = subprocess.run(['node', '-e', script, str(source)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

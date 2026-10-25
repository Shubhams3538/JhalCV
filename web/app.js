'use strict';
const $ = s => document.querySelector(s);
const el = (tag, props = {}, children = []) => {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === 'text') node.textContent = v;
    else if (k === 'class') node.className = v;
    else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
    else if (k in node) node[k] = v;
    else node.setAttribute(k, v);
  }
  for (const child of children) node.append(child);
  return node;
};
let state, profile, selected = new Set(), draft = null, imported = null, busy = false;
let noticeTimer, viewRevision = 0;
function notify(message, error = false) {
  clearTimeout(noticeTimer);
  const notice = $('#notice');
  notice.hidden = false;
  notice.textContent = message;
  notice.className = error ? 'error' : '';
  notice.title = 'Click to dismiss';
  notice.onclick = () => { clearTimeout(noticeTimer); notice.hidden = true; };
  // Errors stay a little longer so there is time to read them.
  noticeTimer = setTimeout(() => { notice.hidden = true; }, error ? 8000 : 5000);
}
async function api(path, body) {
  const response = await fetch('/api/' + path, body === undefined ? {} : {
    method: 'POST', headers: {'Content-Type': 'application/json', 'X-App-Token': state.token}, body: JSON.stringify(body)
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || 'Request failed.');
  return result;
}
async function task(button, action) {
  button = button.closest('button');
  if (busy) return;
  busy = true;
  const controls = [...document.querySelectorAll('button, input, textarea, select')];
  const previous = controls.map(b => b.disabled);
  controls.forEach(b => b.disabled = true);
  const old = button.textContent;
  button.textContent = 'Working…';
  try { await action(); } catch (e) { notify(e.message, true); }
  finally {
    busy = false;
    controls.forEach((b, i) => b.disabled = previous[i]);
    button.textContent = old;
  }
}
function invalidate() {
  draft = null;
  $('#draft-preview').replaceChildren();
  $('#draft-preview').hidden = true;
  $('#empty-preview').hidden = false;
}
function field(label, value, change, {area = false, wide = false, placeholder = ''} = {}) {
  const id = 'f-' + crypto.randomUUID();
  const input = el(area ? 'textarea' : 'input', {id, value: value ?? '', placeholder, rows: area ? 3 : undefined,
    oninput: e => {change(e.target.value); invalidate();}});
  return el('div', {class: wide ? 'wide' : ''}, [el('label', {htmlFor: id, text: label}), input]);
}
const commaList = s => s.split(',').map(x => x.trim()).filter(Boolean);
function bulletEditor(entry, allowPlanned = true) {
  const wrap = el('div', {class: 'wide bullet-editor'});
  function render() {
    wrap.replaceChildren(el('p', {class: 'hint', text: allowPlanned ? 'Each bullet keeps its own status. Planned project work never appears on the resume. At least two shipped bullets are required per project.' : 'Experience renders exactly as saved. Add only confirmed work.'}));
    entry.bullets.forEach((bullet, index) => {
      const row = el('div', {class: 'bullet-row'});
      row.append(field('Bullet ' + (index + 1), bullet.text, value => bullet.text = value, {area: true}));
      const id = 'status-' + crypto.randomUUID();
      const status = el('select', {id, onchange: e => {bullet.status = e.target.value; invalidate();}}, [
        el('option', {value: 'shipped', text: 'Shipped / confirmed'}),
        el('option', {value: 'planned', text: 'Planned — excluded from resume'})
      ]);
      status.value = bullet.status || 'shipped';
      if (allowPlanned) row.append(el('label', {htmlFor: id, text: 'Status'}), status);
      row.append(el('button', {type: 'button', class: 'text-button', text: 'Remove bullet', onclick: () => {
          entry.bullets.splice(index, 1); invalidate(); render();
        }}));
      wrap.append(row);
    });
    wrap.append(el('button', {type: 'button', class: 'text-button', text: '+ Add bullet', onclick: () => {
      entry.bullets.push({id: crypto.randomUUID(), text: '', angles: entry.angles || ['backend'], status: allowPlanned ? 'planned' : 'shipped'});
      invalidate(); render();
    }}));
  }
  render();
  return wrap;
}
function focusEditor(project) {
  const options = {agentic: 'AI / agents', backend: 'Backend', systems: 'Systems',
    infra: 'Infrastructure', frontend: 'Frontend', data: 'Data / ML',
    eval: 'Testing / evaluation', dsa: 'DSA', teaching: 'Teaching'};
  const wrap = el('fieldset', {class: 'wide focus-options'}, [el('legend', {text: 'Focus areas'})]);
  for (const [value, name] of Object.entries(options)) {
    const input = el('input', {type: 'checkbox', checked: (project.angles || []).includes(value),
      onchange: e => {
        const values = new Set(project.angles || []);
        e.target.checked ? values.add(value) : values.delete(value);
        project.angles = [...values]; invalidate();
      }});
    wrap.append(el('label', {class: 'focus-option'}, [input, el('span', {text: name})]));
  }
  return wrap;
}
function projectEditor(p) {
  return el('div', {class: 'form-grid'}, [
    field('Project name', p.name, v => p.name = v),
    field('Technologies (comma separated)', (p.stack || []).join(', '), v => p.stack = commaList(v)),
    field('GitHub / project URL', p.link?.url || '', v => {if (v.trim()) p.link = {label: 'GitHub', url: v}; else delete p.link;}),
    focusEditor(p),
    bulletEditor(p)
  ]);
}
function renderProjects() {
  $('#project-count').textContent = profile.projects.length + ' PROJECTS';
  const list = $('#project-list'); list.replaceChildren();
  profile.projects.forEach(p => {
    const check = el('input', {type: 'checkbox', checked: selected.has(p.id), 'aria-label': 'Consider ' + p.name,
      onclick: e => e.stopPropagation(), onchange: e => {e.target.checked ? selected.add(p.id) : selected.delete(p.id); invalidate();}});
    const summary = el('summary', {}, [check, el('span', {class: 'project-icon', text: (p.name || '?').trim().charAt(0).toUpperCase() || '?'}),
      el('span', {class: 'project-info'}, [el('strong', {text: p.name}), el('small', {text: (p.stack || []).join(' · ')})]), el('span', {text: '▾'})]);
    const card = el('details', {class: 'project-card'}, [summary, projectEditor(p)]);
    card.append(el('p', {class: 'hint', text: 'Choose the focus areas that describe the actual project. Saved edits stay available for future jobs.'}));
    card.append(el('button', {class: 'secondary', text: 'Save project edits', onclick: e => task(e.target, async () => {await saveProfile(); renderProjects();})}));
    card.append(el('button', {class: 'danger', text: 'Remove project', onclick: e => task(e.target, async () => {
      state.profile_revision = (await api('projects/remove', {id: p.id, revision: state.profile_revision})).revision;
      profile.projects = profile.projects.filter(x => x.id !== p.id);
      selected.delete(p.id); invalidate(); renderProjects();
      notify('Project removed from your saved library.');
    })}));
    list.append(card);
  });
  if (!profile.projects.length) list.append(el('p', {class: 'empty', text: 'Start with a GitHub link or add a project manually.'}));
}
function linkFields(owner, key = 'links') {
  const wrap = el('div', {class: 'wide'});
  const links = key === 'links' ? owner.links || (owner.links = []) : owner.link ? [owner.link] : [];
  for (const item of links) {
    const row = el('div', {class: 'form-grid'}, [field('Link label', item.label, v => item.label = v), field('Link URL', item.url, v => item.url = v)]);
    row.append(el('button', {type: 'button', class: 'text-button', text: 'Remove link', onclick: () => {
      if (key === 'links') owner.links = owner.links.filter(x => x !== item); else delete owner.link;
      invalidate(); renderProfile();
    }}));
    wrap.append(row);
  }
  if (key === 'links' || !links.length) wrap.append(el('button', {type: 'button', class: 'text-button', text: '+ Add link', onclick: () => {
    if (key === 'links') owner.links.push({label: '', url: ''}); else owner.link = {label: '', url: ''};
    invalidate(); renderProfile();
  }}));
  return wrap;
}
function renderProfile() {
  const container = $('#profile-fields'); container.replaceChildren();
  const basics = el('div', {class: 'form-grid'});
  for (const key of ['name', 'email', 'phone']) basics.append(field(key[0].toUpperCase() + key.slice(1), profile.basics[key], v => profile.basics[key] = v));
  basics.append(linkFields(profile.basics));
  container.append(el('article', {class: 'panel'}, [el('h2', {text: 'Contact details'}), basics]));
  const definitions = {
    experience: {title: 'Experience', fields: ['org', 'role', 'location', 'start', 'end'], fresh: () => ({id: crypto.randomUUID(), org: '', role: '', location: '', start: '', end: '', bullets: [], stack: []})},
    education: {title: 'Education', fields: ['org', 'degree', 'start', 'end', 'detail'], fresh: () => ({org: '', degree: '', start: '', end: '', detail: ''})},
    coding_profiles: {title: 'Coding profiles', fields: ['text'], fresh: () => ({text: ''})}
  };
  for (const [section, def] of Object.entries(definitions)) {
    const panel = el('article', {class: 'panel'}, [el('h2', {text: def.title})]);
    profile[section].forEach((entry, index) => {
      const grid = el('div', {class: 'form-grid entry'});
      for (const key of def.fields) grid.append(field(({org: 'Organization', start: 'Start date', end: 'End date', text: 'Description'})[key] || key[0].toUpperCase() + key.slice(1), entry[key], v => entry[key] = v));
      if (section === 'experience') grid.append(bulletEditor(entry, false));
      if (section === 'coding_profiles') grid.append(linkFields(entry, 'link'));
      grid.append(el('button', {type: 'button', class: 'text-button', text: 'Remove entry', onclick: () => {profile[section].splice(index, 1); invalidate(); renderProfile();}}));
      panel.append(grid);
    });
    panel.append(el('button', {type: 'button', class: 'text-button', text: '+ Add entry', onclick: () => {profile[section].push(def.fresh()); invalidate(); renderProfile();}}));
    container.append(panel);
  }
  const skills = el('article', {class: 'panel'}, [el('h2', {text: 'Skills'})]);
  for (const [group, items] of Object.entries(profile.skills)) {
    if (group.startsWith('_') || !Array.isArray(items)) continue;
    const row = field(group + ' (comma separated)', items.join(', '), v => profile.skills[group] = commaList(v));
    row.append(el('button', {type: 'button', class: 'text-button', text: 'Remove group', onclick: () => {delete profile.skills[group]; invalidate(); renderProfile();}}));
    skills.append(row);
  }
  const groupName = el('input', {placeholder: 'New skill group, e.g. Languages', 'aria-label': 'New skill group'});
  skills.append(el('div', {class: 'import-row'}, [groupName, el('button', {type: 'button', class: 'secondary', text: 'Add group', onclick: () => {
    const name = groupName.value.trim(); if (!name || name.startsWith('_') || Object.hasOwn(profile.skills, name)) return notify('Enter a unique skill group name.', true);
    Object.defineProperty(profile.skills, name, {value: [], enumerable: true, configurable: true, writable: true}); invalidate(); renderProfile();
  }})]));
  container.append(skills);
}
async function storeProfile(value) {
  state.profile_revision = (await api('profile', {profile: value, revision: state.profile_revision})).revision;
}
async function saveProfile() {
  await storeProfile(profile);
  state.example = false;
  notify('Profile saved on this computer.');
}
const VIEW_LABELS = {workspace: 'Tailor resume', jobs: 'Jobs', profile: 'My profile', insights: 'Insights', usage: 'Usage', benchmarks: 'Benchmarks', settings: 'Settings'};
function rememberView(view, replace = false) {
  const hash = '#' + view + (view === 'jobs' ? '/' + jobsTab : '');
  if (location.hash !== hash) history[replace ? 'replaceState' : 'pushState'](null, '', hash);
}
function restoreView() {
  const [requested, tab] = location.hash.slice(1).split('/');
  const view = Object.hasOwn(VIEW_LABELS, requested) ? requested : 'workspace';
  if (view === 'jobs') jobsTab = JOB_TABS.some(([id]) => id === tab) ? tab : 'eligible';
  rememberView(view, true);
  showView(view, false);
}
function showView(view, remember = true, onJobs = null) {
  const revision = ++viewRevision;
  if (!Object.hasOwn(VIEW_LABELS, view)) view = 'workspace';
  if (remember) rememberView(view);
  document.querySelectorAll('.view').forEach(v => v.hidden = v.id !== view);
  document.querySelectorAll('.nav').forEach(b => b.classList.toggle('active', b.dataset.view === view));
  $('#breadcrumb').textContent = VIEW_LABELS[view];
  if (view === 'workspace') renderProjects();
  if (view === 'profile') renderProfile();
  if (view === 'jobs') api('jobs').then(data => {if (revision === viewRevision) (onJobs || renderJobs)(data);}).catch(e => notify(e.message, true));
  if (view === 'benchmarks') loadBenchmark();
  if (view === 'usage') api('usage').then(renderUsage).catch(e => notify(e.message, true));
  if (view === 'insights') api('insights').then(renderInsights).catch(e => notify(e.message, true));
}
let jobsData = null, jobsTab = 'eligible', jobsPoll = null, refreshPick = new Set();
let companyImport = [];
function renderCompanyImport(data) {
  companyImport = data.rows;
  $('#jobs-import-summary').textContent = `${data.ready} ready · ${data.duplicates} duplicates skipped · ${data.invalid} invalid`;
  const list = $('#jobs-import-rows'); list.replaceChildren();
  for (const row of data.rows) {
    const pick = el('input', {type: 'checkbox', checked: row.status === 'ready', disabled: row.status !== 'ready',
      'aria-label': `Import ${row.company || 'row ' + row.row}`, onchange: () => {row.selected = pick.checked; updateCompanyImport();}});
    row.selected = pick.checked;
    list.append(el('div', {class: 'csv-import-row'}, [pick, el('div', {}, [
      el('strong', {text: row.company || `Row ${row.row}`}), el('small', {text: row.url || row.message}),
      ...(row.url && row.message ? [el('small', {text: row.message})] : [])
    ])]));
  }
  updateCompanyImport();
  $('#jobs-import-dialog').showModal();
}
function updateCompanyImport() {
  const count = companyImport.filter(row => row.selected).length;
  $('#jobs-import-confirm').textContent = `Import ${count} companies`;
  $('#jobs-import-confirm').disabled = !count;
}
const JOB_TABS = [['eligible', 'Eligible'], ['no', 'Not eligible'], ['applied', 'Applied']];
function jobInTab(job, tab) {
  return job.category === tab;
}

function ago(seconds) {
  if (!seconds) return 'never';
  const mins = Math.round((Date.now() / 1000 - seconds) / 60);
  if (mins < 1) return 'just now';
  if (mins < 60) return mins + ' min ago';
  if (mins < 48 * 60) return Math.round(mins / 60) + ' h ago';
  return Math.round(mins / 1440) + ' days ago';
}
function renderJobs(data) {
  jobsData = data;
  const running = data.progress.running;
  $('#jobs-source-count').textContent = data.sources.length + (data.sources.length === 1 ? ' COMPANY' : ' COMPANIES');
  const sources = $('#jobs-sources'); const sourceScroll = sources.scrollTop; sources.replaceChildren();
  refreshPick = new Set([...refreshPick].filter(id => data.sources.some(s => s.id === id)));
  for (const src of data.sources) {
    const status = src.last_error ? el('small', {class: 'job-error', text: src.last_error})
      : el('small', {text: src.last_run_at ? `${src.last_count} matching job(s) · refreshed ${ago(src.last_run_at)}` : 'Not refreshed yet'});
    const locationInput = el('input', {type: 'text', value: src.locations || '', maxLength: 500,
      placeholder: 'India, Remote', 'aria-label': `Locations to keep for ${src.name}`});
    const locationForm = el('form', {class: 'source-location-form', onsubmit: e => {
      e.preventDefault();
      task(e.submitter, async () => {
        renderJobs(await api('jobs/sources/locations', {id: src.id, locations: locationInput.value}));
        notify('Location filter saved. Refresh to collect matching jobs.');
      });
    }}, [locationInput, el('button', {type: 'submit', class: 'text-button', text: 'Save locations', disabled: running})]);
    const urlInput = el('input', {type: 'url', value: src.url, required: true, maxLength: 3000, 'aria-label': `Career URL for ${src.name}`});
    const editLink = el('details', {}, [el('summary', {text: 'Edit career link'}), el('form', {class: 'source-location-form', onsubmit: e => {
      e.preventDefault();
      task(e.submitter, async () => {renderJobs(await api('jobs/sources/url', {id: src.id, url: urlInput.value})); notify('Career link updated. Saved jobs are retained.');});
    }}, [urlInput, el('button', {type: 'submit', class: 'text-button', text: 'Save link', disabled: running})])]);
    const pick = el('input', {type: 'checkbox', checked: refreshPick.has(src.id), disabled: running,
      'aria-label': `Include ${src.name} in Refresh selected`, onchange: e => {
        e.target.checked ? refreshPick.add(src.id) : refreshPick.delete(src.id); syncRefreshSelected();
      }});
    sources.append(el('div', {class: 'source-row'}, [
      el('div', {class: 'source-info'}, [el('label', {class: 'source-pick'}, [pick, el('strong', {text: src.name})]), el('a', {href: src.url, target: '_blank', rel: 'noopener', text: src.url}), status, locationForm, editLink]),
      el('div', {class: 'source-actions'}, [el('span', {class: 'pill', text: src.label.toUpperCase()}),
      el('button', {type: 'button', class: 'secondary', text: 'Refresh ↻', disabled: running, title: `Refresh only ${src.name}`,
        onclick: e => task(e.target, async () => renderJobs(await api('jobs/refresh', {sources: [src.id]})))}),
      el('button', {type: 'button', class: 'text-button', text: 'Remove', disabled: running, onclick: e => {
        if (!confirm(`Remove ${src.name} and its collected jobs?`)) return;
        task(e.target, async () => renderJobs(await api('jobs/sources/remove', {id: src.id})));
      }})])]));
  }
  if (!data.sources.length) sources.append(el('p', {class: 'empty', text: 'No careers pages yet. Add one above.'}));
  sources.scrollTop = sourceScroll;
  const exp = data.experience;
  if (document.activeElement !== $('#jobs-years')) $('#jobs-years').value = exp.years;
  $('#experience-pill').textContent = `Experience · ${Number(exp.years ?? 0)} yrs`;
  $('#jobs-years-hint').textContent = exp.custom
    ? `Using your own value. Your profile dates add up to ${exp.computed} years.`
    : `Calculated from your profile's experience dates (${exp.computed} years). Change it if some roles should not count.`;
  $('#jobs-years-reset').hidden = !exp.custom;
  const agents = data.agents.length ? data.agents.join(' → ') : 'no agent installed (text rules only)';
  const active = (data.progress.active || []).join(', ');
  const progress = data.progress.total != null
    ? `${data.progress.completed || 0}/${data.progress.total} ${data.progress.phase === 'review' ? 'jobs reviewed' : 'companies completed'}${active ? ' · Active: ' + active : ''}. ${data.progress.message}`
    : data.progress.message;
  $('#jobs-progress').textContent = running ? progress
    : data.progress.error ? 'Last refresh failed: ' + data.progress.error
    : (data.progress.message ? data.progress.message + ' ' : '') + `Last refresh ${ago(Number(data.last_refresh))}. Relevance and experience: ${agents}.`;
  $('#jobs-progress').className = data.progress.error && !running ? 'warning' : 'hint';
  $('#jobs-refresh').disabled = running;
  $('#jobs-refresh').textContent = running && data.progress.phase !== 'review' ? 'Refreshing…' : 'Refresh all ↻';
  const pending = data.unreviewed_count ?? data.jobs.filter(j => !j.relevant || !j.eligible).length;
  $('#jobs-review-all').disabled = running || pending === 0;
  $('#jobs-review-all').textContent = running && data.progress.phase === 'review'
    ? 'Reviewing saved jobs…' : `Review unreviewed jobs (${pending})`;
  syncRefreshSelected();
  const tabs = $('#jobs-tabs'); tabs.replaceChildren();
  for (const [id, name] of JOB_TABS) {
    const count = data.jobs.filter(j => jobInTab(j, id)).length;
    tabs.append(el('button', {type: 'button', role: 'tab', class: 'job-tab' + (id === jobsTab ? ' active' : ''), 'aria-selected': String(id === jobsTab),
      text: `${name} ${count}`, onclick: () => {jobsTab = id; rememberView('jobs'); renderJobs(jobsData);}}));
  }
  $('#jobs-review-count').textContent = data.jobs.filter(j => jobInTab(j, 'review')).length;
  $('#jobs-old-count').textContent = data.jobs.filter(j => jobInTab(j, 'old')).length;
  $('#jobs-review').hidden = jobsTab !== 'eligible';
  $('#jobs-old').hidden = jobsTab !== 'eligible';
  renderJobList();
  clearTimeout(jobsPoll);
  if (running) jobsPoll = setTimeout(() => api('jobs').then(renderJobs).catch(() => {}), 2000);
}
function syncRefreshSelected() {
  const button = $('#jobs-refresh-selected'), count = refreshPick.size;
  button.disabled = !count || Boolean(jobsData?.progress.running);
  button.textContent = count ? `Refresh selected (${count})` : 'Refresh selected';
}
function renderJobList() {
  const data = jobsData, query = $('#jobs-search').value.trim().toLowerCase();
  const openDetails = new Set([...document.querySelectorAll('.job-row details[open]')].map(d => `${d.closest('.job-row').dataset.jobId}:${d.className}`));
  for (const [group, selector] of [[jobsTab, '#jobs-list'], ['review', '#jobs-review-list'], ['old', '#jobs-old-list']]) {
    const jobs = data.jobs.filter(j => jobInTab(j, group) && (!query || `${j.title} ${j.company} ${j.location}`.toLowerCase().includes(query)));
    $('#jobs-count').textContent = data.jobs.length + ' SAVED JOBS';
    const list = $(selector); list.replaceChildren();
    if (!jobs.length) list.append(el('p', {class: 'empty', text: data.jobs.length ? 'No jobs in this section.' : 'Jobs appear here after a refresh.'}));
    const verdicts = {yes: ['ELIGIBLE', 'owned'], no: ['NOT ELIGIBLE', 'ineligible'], unclear: ['UNCLEAR', '']};
    for (const job of jobs) {
      const [label, tone] = job.manual_eligible === 'no' ? ['NOT ELIGIBLE · YOU', 'ineligible'] : job.relevant === 'no' ? ['NOT RELEVANT', 'missing']
        : !job.relevant ? ['AWAITING REVIEW', ''] : verdicts[job.eligible] || ['CHECKING', ''];
      const setStatus = status => e => task(e.target, async () => renderJobs(await api('jobs/status', {id: job.id, status})));
      const meta = [job.company, job.location, job.freshness === 'unknown' ? 'Posting date unverified' : job.posted && 'posted ' + job.posted, job.active === 0 && 'Not returned in latest refresh'].filter(Boolean).join(' · ');
      const actions = el('div', {class: 'job-actions'}, [el('button', {type: 'button', class: 'secondary', text: 'Tailor resume', onclick: e => task(e.target, () => tailorForJob(job))})]);
      const more = el('details', {class: 'job-more'}, [el('summary', {text: 'More', 'aria-label': `More actions for ${job.title}`})]);
      if (openDetails.has(`${job.id}:job-more`)) more.open = true;
      const menu = el('div', {class: 'job-menu'}); more.append(menu); actions.append(more);
      if (job.status !== 'applied') menu.append(el('button', {type: 'button', class: 'text-button', text: job.manual_eligible === 'no' ? 'Undo not eligible' : 'Mark not eligible', onclick: e => task(e.target, async () => renderJobs(await api('jobs/eligibility', {id: job.id, eligible: job.manual_eligible === 'no' ? null : 'no'})))}));
      if (job.status === 'hidden') menu.append(el('button', {type: 'button', class: 'text-button', text: 'Restore', onclick: setStatus('seen')}));
      else {
        menu.append(job.status === 'applied'
          ? el('button', {type: 'button', class: 'text-button', text: 'Not applied', onclick: setStatus('seen')})
          : el('button', {type: 'button', class: 'text-button', text: 'Mark applied', onclick: setStatus('applied')}));
      }
      menu.append(el('button', {type: 'button', class: 'text-button', text: 'Delete', onclick: e => {
        if (!confirm('Delete this saved job? It will not be collected again from this careers page.')) return;
        task(e.target, async () => renderJobs(await api('jobs/delete', {id: job.id})));
      }}));
      list.append(el('div', {class: 'job-row', 'data-job-id': String(job.id)}, [
        el('div', {class: 'job-info'}, [
          el('div', {class: 'job-title'}, [el('a', {href: job.url, target: '_blank', rel: 'noopener', text: job.title}),
            ...(job.status === 'new' ? [el('span', {class: 'job-new', text: 'NEW'})] : [])]),
          el('small', {text: meta}),
          ...(job.manual_eligible === 'no' ? [el('small', {text: 'You marked this job not eligible.'})] : []),
          ...(job.relevance_reason || job.reason ? [el('details', {class: 'job-explanation', ...(openDetails.has(`${job.id}:job-explanation`) ? {open: true} : {})}, [
            el('summary', {text: 'AI assessment'}),
            ...(job.relevance_reason ? [el('small', {class: 'job-reason', text: 'Relevance: ' + job.relevance_reason})] : []),
            ...(job.reason ? [el('small', {class: 'job-reason', text: job.reason + (job.checked_by === 'rules' ? ' (text rules)' : '')})] : [])])] : [])]),
        el('span', {class: 'pill ' + tone, text: label}), actions]));
    }
  }
}
let tailoringJob = null;
function renderTailoringJob() {
  $('#tailor-job-context').hidden = !tailoringJob;
  if (!tailoringJob) return;
  $('#tailor-job-title').textContent = `${tailoringJob.title} · ${tailoringJob.company}`;
  const link = $('#tailor-job-link');
  try {
    const url = new URL(tailoringJob.url);
    if (!['http:', 'https:'].includes(url.protocol)) throw new Error();
    link.href = url.href; link.hidden = false;
  } catch {link.removeAttribute('href'); link.hidden = true;}
}
function saveTailoringJob() {
  try {sessionStorage.setItem('jhalcv-tailoring-job', JSON.stringify(tailoringJob));} catch {}
  renderTailoringJob();
}
function returnToTailoredJob() {
  if (!tailoringJob) return;
  const context = tailoringJob;
  jobsTab = JOB_TABS.some(([id]) => id === context.tab) ? context.tab : 'eligible';
  $('#jobs-search').value = context.search || '';
  showView('jobs', true, data => {
    const job = data.jobs.find(j => j.id === context.id);
    if (job && !jobInTab(job, jobsTab)) jobsTab = ['review', 'old'].includes(job.category) ? 'eligible' : job.category;
    if (job && !`${job.title} ${job.company} ${job.location}`.toLowerCase().includes($('#jobs-search').value.trim().toLowerCase())) $('#jobs-search').value = '';
    rememberView('jobs', true);
    renderJobs(data);
    $('#jobs-review').open = Boolean(context.reviewOpen || job?.category === 'review');
    $('#jobs-old').open = Boolean(context.oldOpen || job?.category === 'old');
    $('#jobs-sources').scrollTop = context.sourceScroll || 0;
    window.scrollTo(0, context.scrollY || 0);
    const row = [...document.querySelectorAll('.job-row')].find(row => row.dataset.jobId === String(context.id));
    if (row && job) {
      row.classList.add('returned-job');
      const rect = row.getBoundingClientRect();
      if (rect.top < 0 || rect.bottom > innerHeight) row.scrollIntoView({block: 'center'});
      row.tabIndex = -1; row.focus({preventScroll: true});
    } else notify('This job is no longer in the saved collection. Your Jobs view has been restored.');
  });
}
async function tailorForJob(job) {
  const context = {id: job.id, title: job.title, company: job.company, url: job.url, tab: jobsTab,
    search: $('#jobs-search').value, scrollY: window.scrollY, sourceScroll: $('#jobs-sources').scrollTop,
    reviewOpen: $('#jobs-review').open, oldOpen: $('#jobs-old').open};
  const detail = await api('jobs/detail', {id: job.id});
  if (!detail.description.trim()) throw new Error('This job has no description saved yet. Refresh, or open the job and paste its description.');
  $('#jd').value = `${detail.title} at ${detail.company}${detail.location ? ' (' + detail.location + ')' : ''}\n\n${detail.description}`.slice(0, 30000);
  $('#jd').dispatchEvent(new Event('input'));
  if (job.status === 'new') renderJobs(await api('jobs/status', {id: job.id, status: 'seen'}));
  tailoringJob = context; saveTailoringJob();
  showView('workspace'); window.scrollTo(0, 0);
  notify('Job description added. Choose your projects, then create the tailored draft.');
}
function insightBar(item) {
  const fill = el('span'); fill.style.width = item.percent + '%';
  return el('div', {class: 'insight-bar', 'aria-hidden': 'true'}, [fill]);
}
function insightStat(item, jobs) {
  return el('small', {class: 'insight-stat', text: `${item.count} of ${jobs} job${jobs === 1 ? '' : 's'} · ${item.percent}%`});
}
function renderInsights(data) {
  const jobs = data.jobs;
  $('#insights-summary').textContent = jobs
    ? `${jobs} job${jobs === 1 ? '' : 's'} analyzed. Skills these jobs asked for that your profile does not list anywhere. Add the ones you can genuinely speak to.`
    : 'No jobs analyzed yet. Each tailored draft adds its job description here.';
  $('#insights-unhide').hidden = !data.hidden;
  $('#insights-unhide').textContent = `Show ${data.hidden} hidden skill${data.hidden === 1 ? '' : 's'}`;
  const groups = Object.entries(profile.skills).filter(([k, v]) => !k.startsWith('_') && Array.isArray(v)).map(([k]) => k);
  const missing = $('#insights-missing'); missing.replaceChildren();
  if (jobs && !data.missing.length) missing.append(el('p', {class: 'empty', text: 'Nothing missing. Your profile covers every skill these jobs asked for.'}));
  for (const item of data.missing) {
    const group = el('select', {'aria-label': 'Skill group for ' + item.skill},
      groups.map(g => el('option', {value: g, text: g})));
    const add = el('button', {type: 'button', class: 'secondary', text: 'Add to skills', disabled: !groups.length, onclick: e => task(e.target, async () => {
      if (state.example) throw new Error('Replace the sample profile in My profile and save it first.');
      await storeProfile(profile);
      const result = await api('insights/add', {skill: item.skill, group: group.value});
      profile = result.profile; state.profile_revision = result.revision; invalidate(); renderInsights(result.insights);
      notify(`${item.skill} added to ${group.value}. The AI can use it from your next resume.`);
    })});
    const hide = el('button', {type: 'button', class: 'text-button', text: 'Hide', onclick: e => task(e.target, async () => {
      renderInsights((await api('insights/hide', {skill: item.key})).insights);
    })});
    missing.append(el('div', {class: 'insight-row'}, [
      el('div', {class: 'insight-name'}, [el('strong', {text: item.skill}), insightStat(item, jobs)]), insightBar(item),
      el('div', {class: 'insight-actions'}, [group, add, hide])]));
  }
  const asked = $('#insights-asked'); asked.replaceChildren();
  if (!jobs) asked.append(el('p', {class: 'empty', text: 'Tailor a resume to start building this list.'}));
  for (const item of data.asked) {
    asked.append(el('div', {class: 'insight-row'}, [
      el('div', {class: 'insight-name'}, [el('strong', {text: item.skill}), insightStat(item, jobs)]), insightBar(item),
      el('span', {class: 'pill ' + (item.owned ? 'owned' : 'missing'), text: item.owned ? 'IN PROFILE' : 'MISSING'})]));
  }
}
function fillModelSelect(select, providerId, current) {
  const provider = state.credentials.providers.find(p => p.id === providerId);
  const models = provider.models || [{id: provider.default_model, name: provider.default_model}];
  select.replaceChildren(...models.map(model => el('option', {
    value: model.id, text: model.name + (model.id === provider.default_model ? ' — Default' : '')
  })), el('option', {value: '__custom__', text: 'Other model…'}));
  select.value = models.some(model => model.id === current) ? current : '__custom__';
}
function modelEditor(key) {
  const select = el('select', {'aria-label': 'Model for ' + key.label});
  const custom = el('input', {value: key.model, maxLength: 150, placeholder: 'Custom model ID', 'aria-label': 'Custom model ID for ' + key.label});
  fillModelSelect(select, key.provider, key.model);
  const sync = () => {custom.hidden = select.value !== '__custom__';};
  select.onchange = sync; sync();
  return el('details', {class: 'key-model-editor'}, [el('summary', {text: 'Change model'}), select, custom,
    el('button', {type: 'button', class: 'secondary', text: 'Save model', onclick: e => task(e.target, async () => {
      const model = select.value === '__custom__' ? custom.value : select.value;
      state.credentials = await api('keys/model', {id: key.id, model});
      renderKeys(); invalidate(); notify('Model saved.');
    })})]);
}
function updateAiStatus() {
  $('#ai-status').textContent = state.ai_available && state.settings.tailorer !== 'rules'
    ? 'AI available · automatic key failover' : 'Rules mode · your original wording';
}
function renderKeys() {
  const info = state.credentials;
  state.ai_available = info.keys.length > 0;
  updateAiStatus();
  $('#api-reminder-enabled').checked = !info.reminder_dismissed;
  const list = $('#api-key-list'); list.replaceChildren();
  if (!info.keys.length) list.append(el('p', {class: 'empty', text: 'No keys configured. Rules mode is ready to use.'}));
  for (const key of info.keys) {
    const provider = info.providers.find(p => p.id === key.provider);
    const details = el('div', {class: 'key-details'}, [el('strong', {text: key.label}),
      el('span', {class: 'key-provider', text: provider.name + ' · ' + key.model}),
      el('code', {class: 'key-mask', text: '••••••••'}), modelEditor(key)]);
    const actions = el('div', {class: 'key-actions'});
    if (key.first) actions.append(el('span', {class: 'pill', text: 'TRY FIRST'}));
    else actions.append(el('button', {type: 'button', class: 'text-button', text: 'Try first', onclick: e => task(e.target, async () => {
      state.credentials = await api('keys/prefer', {id: key.id}); renderKeys(); invalidate(); notify('Key priority saved.');
    })}));
    actions.append(el('button', {type: 'button', class: 'text-button remove-key', text: key.source === 'environment' ? 'Ignore' : 'Remove', onclick: e => task(e.target, async () => {
      state.credentials = await api('keys/remove', {id: key.id}); renderKeys(); invalidate(); notify('API-key entry removed from JhalCV.');
    })}));
    list.append(el('div', {class: 'key-card'}, [el('span', {class: 'key-provider-icon', text: provider.name[0]}), details, actions]));
  }
}
function setupKeys() {
  const info = state.credentials;
  $('#api-provider').replaceChildren(...info.providers.map(p => el('option', {value: p.id, text: p.name})));
  const syncCustomModel = () => {
    const custom = $('#api-key-model').value === '__custom__';
    $('#api-custom-model-field').hidden = !custom;
    $('#api-custom-model').required = custom;
  };
  $('#api-key-model').onchange = syncCustomModel;
  const setModel = () => {
    const provider = state.credentials.providers.find(p => p.id === $('#api-provider').value);
    fillModelSelect($('#api-key-model'), provider.id, provider.default_model);
    $('#api-custom-model').value = ''; syncCustomModel();
  };
  $('#api-provider').onchange = setModel;
  setModel(); renderKeys();
  $('#api-key-form').onsubmit = e => {
    e.preventDefault();
    task(e.submitter, async () => {
      const input = $('#api-key-secret');
      const key = input.value; input.value = '';
      state.credentials = await api('keys/add', {provider: $('#api-provider').value,
        label: $('#api-key-label').value, model: $('#api-key-model').value === '__custom__' ? $('#api-custom-model').value : $('#api-key-model').value, key});
      $('#api-key-label').value = ''; renderKeys(); invalidate(); notify('API key saved locally. Only a dummy mask is displayed.');
    });
  };
  $('#api-reminder-enabled').onchange = e => task($('#api-key-form button[type=submit]'), async () => {
    try {
      state.credentials = await api('keys/reminder', {dismissed: !e.target.checked});
      renderKeys(); notify('Startup reminder preference saved.');
    } catch (error) {renderKeys(); throw error;}
  });
  const reminder = $('#api-key-reminder');
  $('#reminder-add-key').onclick = () => {reminder.close(); showView('settings'); $('#api-panel').scrollIntoView({behavior: 'smooth'}); $('#api-provider').focus();};
  $('#reminder-later').onclick = () => reminder.close();
  $('#reminder-never').onclick = e => task(e.target, async () => {
    try {
      state.credentials = await api('keys/reminder', {dismissed: true});
      renderKeys(); reminder.close(); notify('API-key startup reminders turned off.');
    } catch (error) {
      $('#api-reminder-error').hidden = false; $('#api-reminder-error').textContent = error.message;
      throw error;
    }
  });
  if (info.show_reminder) reminder.showModal();
}
function renderUsage(data) {
  const n = value => Number(value).toLocaleString();
  const usd = value => '$' + Number(value).toFixed(6);
  const t = data.total, day = data.today;
  $('#usage-period').textContent = `Since ${new Date(data.started_at).toLocaleString()} · Today: ${n(day.input_tokens + day.output_tokens)} tokens · ${usd(day.cost_usd)} combined valuation`;
  const metrics = [
    ['Total measured tokens', n(t.input_tokens + t.output_tokens)],
    ['Input / output', `${n(t.input_tokens)} / ${n(t.output_tokens)}`],
    ['Cached input', n(t.cached_input_tokens)],
    ['Combined USD valuation' + (t.unpriced ? ' (partial)' : ''), usd(t.cost_usd)],
    ['API cost estimate', usd(t.api_cost_usd)],
    ['Subscription equivalent', usd(t.subscription_cost_usd)],
    ['AI attempts / turns', n(t.calls)],
    ['Local cache hits · no new tokens', n(t.cache_hits)]
  ];
  $('#usage-metrics').replaceChildren(...metrics.map(([label, value]) => el('div', {class: 'usage-metric'}, [el('small', {text: label}), el('strong', {text: value})])));
  const warnings = [];
  if (t.unmeasured) warnings.push(`${n(t.unmeasured)} attempts did not report complete token counts; token totals are partial.`);
  if (t.unpriced) warnings.push(`${n(t.unpriced)} attempts could not be priced; dollar totals are a partial subtotal.`);
  if (data.accounting_error) warnings.push('Some usage could not be saved. Totals may be incomplete.');
  $('#usage-warning').hidden = !warnings.length;
  $('#usage-warning').textContent = warnings.join(' ');
  const rows = data.groups.map(g => el('div', {class: 'usage-model'}, [
    el('div', {}, [el('strong', {text: `${g.provider} · ${g.model}`}), el('small', {text: `${g.channel === 'subscription' ? 'Subscription equivalent' : 'API estimate / reported charge'}${g.provider === 'codex' ? ' · valued as ' + g.valuation_model : ''}`})]),
    el('div', {}, [el('strong', {text: `${n(g.input_tokens + g.output_tokens)} tokens · ${usd(g.cost_usd)}${g.unpriced ? ' (partial)' : ''}`}), el('small', {text: `${n(g.input_tokens)} input · ${n(g.output_tokens)} output · ${n(g.cached_input_tokens)} cached · ${n(g.calls)} attempts / turns · ${n(g.cache_hits)} local hits`})])
  ]));
  $('#usage-models').replaceChildren(...(rows.length ? rows : [el('p', {class: 'hint', text: 'No AI activity recorded yet. New resume and job AI calls will appear here.'})]));
  $('#usage-reference').replaceChildren(...data.reference_models.map(model => el('option', {value: model, text: model, selected: model === data.codex_reference})));
  $('#usage-rates').textContent = `Rates checked ${data.rates_checked}. Unknown models and unsupported context tiers are left unpriced. Claude CLI values use its own cost estimates when available.`;
  $('#usage-sources').replaceChildren(...Object.entries(data.sources).map(([name, href]) => el('a', {href, text: name + ' pricing / usage', target: '_blank', rel: 'noopener noreferrer'})));
}

let benchmarkReport = null, benchmarkPoll = null, benchmarkRun = '';
async function loadBenchmark() {
  clearTimeout(benchmarkPoll);
  try {
    renderBenchmark(await api('benchmarks' + (benchmarkRun ? '?run=' + encodeURIComponent(benchmarkRun) : '')));
  } catch (e) {notify(e.message, true);}
}
function renderBenchmark(data) {
  const report = data.report;
  benchmarkReport = report;
  const runs = [...data.history];
  if (report && !runs.some(run => run.id === report.id)) runs.unshift(report);
  $('#benchmark-run').replaceChildren(el('option', {value: '', text: 'Latest / active run', selected: !benchmarkRun}), ...runs.map(run => el('option', {value: run.id, selected: benchmarkRun === run.id,
    text: `${new Date(run.started_at).toLocaleString()} · ${run.mode} · ${run.status}`})));
  $('#benchmark-warning').hidden = !data.persistence_warning;
  $('#benchmark-warning').textContent = 'Some timing data could not be saved. Download the current report before restarting.';
  $('#benchmarks-download').disabled = !report;
  $('#benchmark-status').textContent = report ? `${report.mode === 'review' ? 'Saved-job review' : 'Job refresh'} · ${report.status} · ${report.elapsed_seconds.toFixed(2)} seconds elapsed · Started ${new Date(report.started_at).toLocaleString()}`
    : 'No live report yet. Your next Refresh all, Refresh selected, or Review unreviewed jobs run will be recorded automatically.';
  const table = (headings, rows) => el('table', {}, [el('thead', {}, [el('tr', {}, headings.map(text => el('th', {text})))]), el('tbody', {}, rows.map(row => el('tr', {}, row.map(text => el('td', {text: String(text)}))))) ]);
  const sec = value => Number(value || 0).toFixed(3) + 's';
  const stages = report ? [...report.stages].sort((a,b) => b.seconds - a.seconds) : [];
  $('#benchmark-stages').replaceChildren(...(report ? [table(['Stage', 'Calls', 'Accumulated time', 'Slowest call', 'Failures'], stages.map(s => [s.label, s.calls, sec(s.seconds), sec(s.max_seconds), s.failures]))] : []));
  const companies = report ? [...report.companies].sort((a,b) => (b.stages.source?.seconds || 0)-(a.stages.source?.seconds || 0)) : [];
  $('#benchmark-companies').replaceChildren(...(report ? [table(['Company', 'Collection', 'Collection HTTP', 'Collection AI', 'Collection browser', 'Worker wait', 'Failed'], companies.map(c => [c.name, sec(c.stages.source?.seconds), sec(c.stages.http?.seconds), sec(c.stages.ai?.seconds), sec(c.stages.browser?.seconds), sec(c.stages.source_queue?.seconds), c.stages.source_failure?.calls || c.stages.source?.failures || 0]))] : []));
  clearTimeout(benchmarkPoll);
  if (report?.status === 'running' && !$('#benchmarks').hidden) benchmarkPoll = setTimeout(loadBenchmark, 2000);
}

async function init() {
  state = await api('state'); profile = state.profile;
  selected = new Set(profile.projects.map(p => p.id));
  renderProjects(); renderProfile();
  const s = state.settings;
  $('#output-dir').value = s.output_dir; $('#filename').value = s.filename;
  $('#tailoring-mode').value = s.tailorer; $('#model').value = s.model;
  setupKeys();
  try {
    const saved = JSON.parse(sessionStorage.getItem('jhalcv-tailoring-job') || 'null');
    if (saved && Number.isInteger(saved.id) && typeof saved.title === 'string' && typeof saved.company === 'string') tailoringJob = saved;
  } catch {}
  renderTailoringJob();
  $('#back-to-job').onclick = returnToTailoredJob;
  $('#benchmarks-refresh').onclick = loadBenchmark;
  $('#benchmark-run').onchange = e => {benchmarkRun = e.target.value; loadBenchmark();};
  $('#benchmarks-download').onclick = () => {
    if (!benchmarkReport) return;
    const url = URL.createObjectURL(new Blob([JSON.stringify(benchmarkReport, null, 2)], {type: 'application/json'}));
    const link = el('a', {href: url, download: 'jhalcv-benchmark-' + benchmarkReport.id + '.json'});
    document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
  $('#usage-refresh').onclick = e => task(e.target, async () => renderUsage(await api('usage')));
  $('#usage-reference-form').onsubmit = e => {
    e.preventDefault();
    task(e.submitter, async () => {renderUsage(await api('usage/settings', {model: $('#usage-reference').value})); notify('Valuation model saved.');});
  };
  const missing = Object.entries(state.dependencies).filter(([, ok]) => !ok).map(([name]) => name);
  $('#dependencies').textContent = missing.length ? 'Missing PDF tools: ' + missing.join(', ') + '. See the README setup.' : '✓ All PDF tools are installed.';
  $('#dependencies').className = missing.length ? 'warning' : 'hint';
  if (state.example) notify('You’re viewing a sample profile. Open My profile and replace the sample details before creating your resume.');
  document.querySelectorAll('.nav').forEach(b => b.onclick = () => showView(b.dataset.view));
  document.querySelectorAll('.seg-btn').forEach(b => b.onclick = () => {
    document.querySelectorAll('.seg-btn').forEach(x => {
      const on = x === b;
      x.classList.toggle('active', on);
      x.setAttribute('aria-pressed', String(on));
    });
    $('#jobs-board').hidden = b.dataset.pane !== 'board';
    $('#jobs-companies').hidden = b.dataset.pane !== 'companies';
  });
  $('#jd').addEventListener('input', () => {$('#jd-count').textContent = ($('#jd').value.match(/\S+/g) || []).length + ' words'; invalidate();});
  $('#profile-form').onsubmit = e => {e.preventDefault(); task(e.submitter, saveProfile);};
  $('#choose-folder').onclick = e => task(e.target, async () => {
    const chosen = await api('folder', {initial: $('#output-dir').value || state.settings.output_dir});
    if (chosen.path) {
      $('#output-dir').value = chosen.path;
      notify('Folder selected. Save settings to use it for your resumes.');
    }
  });
  $('#settings-form').onsubmit = e => {e.preventDefault(); task(e.submitter, async () => {
    const previous = state.settings;
    state.settings = await api('settings', {output_dir: $('#output-dir').value, filename: $('#filename').value, tailorer: $('#tailoring-mode').value, model: $('#model').value});
    state.credentials = (await api('state')).credentials; renderKeys();
    if (previous.tailorer !== state.settings.tailorer || previous.model !== state.settings.model) invalidate();
    notify('Settings saved.');
  });};
  $('#manual-project').onclick = () => {
    const id = crypto.randomUUID(); profile.projects.push({id, name: 'New project', stack: [], angles: ['backend'], bullets: [{id: id+'-1', angles: ['backend'], text: 'Describe what the project does.'}, {id: id+'-2', angles: ['backend'], text: 'Describe how you built it.'}]});
    selected.add(id); invalidate(); renderProjects(); $('#project-list details:last-child').open = true;
  };
  $('#import-project').onclick = e => task(e.target, importProject);
  $('#tailor').onclick = e => task(e.target, createDraft);
  $('#jobs-source-form').onsubmit = e => {e.preventDefault(); task(e.submitter, async () => {
    renderJobs(await api('jobs/sources/add', {name: $('#jobs-company').value, url: $('#jobs-url').value,
      include: $('#jobs-include').value, exclude: $('#jobs-exclude').value, locations: $('#jobs-locations').value}));
    for (const id of ['#jobs-company', '#jobs-url', '#jobs-include', '#jobs-exclude', '#jobs-locations']) $(id).value = '';
    $('#add-company').removeAttribute('open');
    notify('Careers page added. Refresh to collect its jobs.');
  });};
  $('#jobs-csv').onchange = async e => {
    const file = e.target.files[0];
    if (!file) return;
    try {
      if (file.size > 500000) throw new Error('Choose a CSV file under 500 KB.');
      renderCompanyImport(await api('jobs/sources/preview', {csv: await file.text()}));
    } catch (error) {notify(error.message, true);}
    finally {e.target.value = '';}
  };
  $('#jobs-import-cancel').onclick = () => $('#jobs-import-dialog').close();
  $('#jobs-import-confirm').onclick = e => task(e.target, async () => {
    const rows = companyImport.filter(row => row.selected).map(({company, url}) => ({company, url}));
    const result = await api('jobs/sources/import', {rows, locations: $('#jobs-import-locations').value});
    $('#jobs-import-dialog').close();
    renderJobs(result.state);
    notify(`${result.imported} companies imported. ${result.skipped} duplicates skipped. Select companies to refresh when ready.`);
  });
  $('#jobs-review-all').onclick = e => task(e.target, async () => renderJobs(await api('jobs/review', {})))
    .finally(() => {if (jobsData) renderJobs(jobsData);});
  $('#jobs-refresh').onclick = e => task(e.target, async () => renderJobs(await api('jobs/refresh', {})));
  $('#jobs-refresh-selected').onclick = e => task(e.target, async () => {
    renderJobs(await api('jobs/refresh', {sources: [...refreshPick]}));
    syncRefreshSelected();
  });
  $('#jobs-experience-form').onsubmit = e => {e.preventDefault(); task(e.submitter, async () => {
    await api('jobs/experience', {years: $('#jobs-years').value});
    renderJobs(await api('jobs/recheck', {})); notify('Experience saved. Rechecking jobs…');
  });};
  $('#jobs-years-reset').onclick = e => task(e.target, async () => {
    await api('jobs/experience', {years: null});
    renderJobs(await api('jobs/recheck', {})); notify('Using your profile dates. Rechecking jobs…');
  });
  $('#jobs-search').oninput = () => jobsData && renderJobList();
  document.querySelectorAll('[data-delete-jobs]').forEach(button => button.onclick = e => {
    const group = button.dataset.deleteJobs || jobsTab;
    const names = {eligible: 'Eligible', no: 'Not eligible', applied: 'Applied', review: 'Needs review', old: 'Old'};
    const count = jobsData?.jobs.filter(j => jobInTab(j, group)).length || 0;
    if (!count) return;
    if (!confirm(`Delete all ${count} jobs in ${names[group]}? This includes jobs hidden by search. Deleted jobs will not return on refresh.`)) return;
    task(e.target, async () => renderJobs(await api('jobs/delete-all', {category: group})));
  });

  window.addEventListener('hashchange', restoreView);
  restoreView();
  $('#insights-unhide').onclick = e => task(e.target, async () => renderInsights((await api('insights/unhide', {})).insights));
  $('#insights-reset').onclick = e => {
    if (!confirm('Clear every analyzed job and hidden skill from Insights? Your profile is not changed.')) return;
    task(e.target, async () => {renderInsights((await api('insights/reset', {})).insights); notify('Insights reset.');});
  };
}
async function importProject() {
  imported = await api('import', {url: $('#github-url').value, jd: $('#jd').value});
  const wrap = $('#import-draft'); wrap.hidden = false;
  wrap.replaceChildren(el('h3', {text: 'Review imported project'}), el('p', {class: 'hint', text: imported.notice}), projectEditor(imported.project));
  const source = el('details', {}, [el('summary', {text: 'Read source README'}), el('pre', {class: 'source-readme', text: imported.project._readme})]);
  wrap.append(source, el('button', {class: 'primary', text: 'Confirm facts & add to library', onclick: e => task(e.target, async () => {
    if (!imported.project.link?.url) throw new Error('Keep the GitHub source URL when confirming an import.');
    if (profile.projects.some(p => p.link?.url === imported.project.link.url)) throw new Error('This repository is already in your library. Edit its existing entry instead.');
    const next = structuredClone(profile); next.projects.push(imported.project);
    await storeProfile(next); profile = next; selected.add(imported.project.id); imported = null;
    wrap.hidden = true; $('#github-url').value = ''; invalidate(); renderProjects(); notify('Project added and saved.');
  })}), el('button', {class: 'text-button', text: 'Discard import', onclick: () => {imported = null; wrap.hidden = true;}}));
}
async function createDraft() {
  if (state.example) throw new Error('Replace the sample profile in My profile and save it first.');
  if ($('#jd').value.trim().length < 40) throw new Error('Paste a job description of at least 40 characters.');
  await saveProfile();
  draft = await api('tailor', {jd: $('#jd').value, projects: [...selected]});
  renderDraft();
  notify('Draft ready. Review the project wording, then preview your PDF.');
}
function renderDraft() {
  $('#empty-preview').hidden = true;
  const wrap = $('#draft-preview'); wrap.hidden = false; wrap.replaceChildren();
  wrap.append(el('div', {class: 'report', text: draft.notice}));
  draft.projects.forEach((p, pi) => {
    const card = el('article', {class: 'panel draft-project'}, [el('h3', {text: p.name}), el('p', {class: 'hint', text: 'Keep each bullet within its original character and word budget.'})]);
    p.bullets.forEach((b, bi) => {
      const input = field('Bullet ' + (bi+1), b, v => {draft.projects[pi].bullets[bi] = v;}, {area: true});
      // Draft edits must not call the profile-change invalidation handler.
      const original = input.querySelector('textarea');
      const area = original.cloneNode(true); area.value = b; area.maxLength = p.budgets[bi].chars;
      area.oninput = () => {draft.projects[pi].bullets[bi] = area.value; clearPdf();};
      original.replaceWith(area);
      input.append(el('div', {class: 'budget', text: `Maximum ${p.budgets[bi].chars} characters / ${p.budgets[bi].words} words`}));
      card.append(input);
    }); wrap.append(card);
  });
  if (draft.gaps.length) wrap.append(el('p', {class: 'warning', text: 'Skills not supported by your profile: ' + draft.gaps.join(', ')}));
  const actions = el('div', {class: 'export-actions'}, [el('button', {class: 'primary', text: 'Build PDF preview ↗', onclick: e => task(e.target, previewPdf)})]);
  wrap.append(actions, el('div', {id: 'pdf-result'}));
}
function clearPdf() {$('#pdf-result')?.replaceChildren();}
async function previewPdf() {
  const result = await api('preview', {id: draft.id, projects: draft.projects.map(p => ({bullets: p.bullets}))});
  const wrap = $('#pdf-result'); wrap.replaceChildren();
  if (result.notices.length) wrap.append(el('div', {class: 'report', text: result.notices.join('\n')}));
  if (result.checks.length) wrap.append(el('p', {class: 'warning', text: 'Text checks to review: ' + result.checks.map(c => c.check + ': ' + c.detail).join('; ')}));
  wrap.append(el('iframe', {title: 'One-page resume PDF preview', src: result.url}));
  const actions = el('div', {class: 'export-actions'}, [el('a', {href: result.url, target: '_blank', rel: 'noopener', text: 'Open PDF ↗'}),
    el('button', {class: 'primary', text: 'Save to destination ↗', onclick: e => task(e.target, async () => {
      const saved = await api('export', {id: draft.id, revision: result.revision});
      notify('Resume saved: ' + saved.path + (saved.archived ? '\nPrevious resume archived: ' + saved.archived : ''));
    })})]);
  wrap.append(actions);
}
init().catch(e => notify('Could not load your workspace: ' + e.message, true));

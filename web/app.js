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
function notify(message, error = false) {
  $('#notice').hidden = false;
  $('#notice').textContent = message;
  $('#notice').className = error ? 'error' : '';
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
    const summary = el('summary', {}, [check, el('span', {class: 'project-icon', text: '⌘'}),
      el('span', {class: 'project-info'}, [el('strong', {text: p.name}), el('small', {text: (p.stack || []).join(' · ')})]), el('span', {text: '⌄'})]);
    const card = el('details', {class: 'project-card'}, [summary, projectEditor(p)]);
    card.append(el('p', {class: 'hint', text: 'Choose the focus areas that describe the actual project. Saved edits stay available for future jobs.'}));
    card.append(el('button', {class: 'secondary', text: 'Save project edits', onclick: e => task(e.target, async () => {await saveProfile(); renderProjects();})}));
    card.append(el('button', {class: 'danger', text: 'Remove project', onclick: () => {profile.projects = profile.projects.filter(x => x.id !== p.id); selected.delete(p.id); invalidate(); renderProjects(); notify('Project removed from this draft. Save your profile to keep the removal.');}}));
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
async function saveProfile() {
  await api('profile', profile);
  state.example = false;
  notify('Profile saved on this computer.');
}
function showView(view) {
  document.querySelectorAll('.view').forEach(v => v.hidden = v.id !== view);
  document.querySelectorAll('.nav').forEach(b => b.classList.toggle('active', b.dataset.view === view));
  $('#breadcrumb').textContent = {workspace: 'Tailor resume', profile: 'My profile', settings: 'Settings'}[view];
  if (view === 'workspace') renderProjects();
  if (view === 'profile') renderProfile();
}
async function init() {
  state = await api('state'); profile = state.profile;
  selected = new Set(profile.projects.map(p => p.id));
  renderProjects(); renderProfile();
  const s = state.settings;
  $('#output-dir').value = s.output_dir; $('#filename').value = s.filename;
  $('#tailoring-mode').value = s.tailorer; $('#model').value = s.model;
  $('#ai-status').textContent = state.ai_available && s.tailorer !== 'rules' ? 'AI available · review before export' : 'Rules mode · your original wording';
  const missing = Object.entries(state.dependencies).filter(([, ok]) => !ok).map(([name]) => name);
  $('#dependencies').textContent = missing.length ? 'Missing PDF tools: ' + missing.join(', ') + '. See the README setup.' : '✓ All PDF tools are installed.';
  $('#dependencies').className = missing.length ? 'warning' : 'hint';
  if (state.example) notify('You’re viewing a sample profile. Open My profile and replace the sample details before creating your resume.');
  document.querySelectorAll('.nav').forEach(b => b.onclick = () => showView(b.dataset.view));
  $('#jd').addEventListener('input', () => {$('#jd-count').textContent = ($('#jd').value.match(/\S+/g) || []).length + ' words'; invalidate();});
  $('#profile-form').onsubmit = e => {e.preventDefault(); task(e.submitter, saveProfile);};
  $('#settings-form').onsubmit = e => {e.preventDefault(); task(e.submitter, async () => {
    state.settings = await api('settings', {output_dir: $('#output-dir').value, filename: $('#filename').value, tailorer: $('#tailoring-mode').value, model: $('#model').value});
    $('#ai-status').textContent = state.ai_available && state.settings.tailorer !== 'rules' ? 'AI available · review before export' : 'Rules mode · your original wording';
    invalidate(); notify('Settings saved.');
  });};
  $('#manual-project').onclick = () => {
    const id = crypto.randomUUID(); profile.projects.push({id, name: 'New project', stack: [], angles: ['backend'], bullets: [{id: id+'-1', angles: ['backend'], text: 'Describe what the project does.'}, {id: id+'-2', angles: ['backend'], text: 'Describe how you built it.'}]});
    selected.add(id); invalidate(); renderProjects(); $('#project-list details:last-child').open = true;
  };
  $('#import-project').onclick = e => task(e.target, importProject);
  $('#tailor').onclick = e => task(e.target, createDraft);
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
    await api('profile', next); profile = next; selected.add(imported.project.id); imported = null;
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
      notify('Resume saved: ' + saved.path);
    })})]);
  wrap.append(actions);
}
init().catch(e => notify('Could not load your workspace: ' + e.message, true));

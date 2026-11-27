'use strict';
let setupDraft = null, setupStep = 0, setupReceipt = null, setupSignature = null, setupBaseRevision = null;
const setupSteps = ['Import resume', 'Choose AI', 'Confirm your profile', 'Job preferences', 'Review & finish'];
function setupPayload() {
  return {profile: setupDraft.profile, preferences: setupDraft.preferences, settings: setupDraft.settings,
    companies: setupDraft.companies, revision: setupBaseRevision};
}
function setupChanged() {
  setupReceipt = null;
  setupSignature = null;
}
async function openSetup(refresh = true) {
  const latest = refresh ? await api('state') : state;
  const info = latest.setup;
  if (!info) throw new Error('Restart JhalCV to load the new setup menu, then reopen Settings.');
  setupBaseRevision = latest.profile_revision;
  state.setup = info; state.credentials = latest.credentials; state.dependencies = latest.dependencies;
  setupDraft = {profile: structuredClone(info.profile), settings: structuredClone(latest.settings),
    preferences: {provider: info.preferences.provider || 'rules', years: info.preferences.years ?? info.experience,
      no_experience: !!info.preferences.no_experience, locations: info.preferences.locations || 'India', include: info.preferences.include || '', exclude: info.preferences.exclude || ''},
    companies: structuredClone(info.companies), text: '', structured: false, profileTouched: false, yearsEdited: false};
  if (state.example) setupDraft.settings.filename = 'Resume';
  setupStep = 0; setupChanged();
  $('#setup-dialog').showModal();
  renderSetup();
}
function setupField(label, value, change, options) {
  return field(label, value, v => {change(v); setupChanged();}, options);
}
function setupButton(text, action, primary = false) {
  return el('button', {type: 'button', class: primary ? 'primary' : 'secondary', text,
    onclick: e => task(e.target, action)});
}
function setupSelect(label, options, value, change) {
  const id = 'setup-' + crypto.randomUUID();
  const select = el('select', {id, onchange: e => {change(e.target.value); setupChanged();}},
    options.map(([key, name, disabled]) => el('option', {value: key, text: name, disabled: !!disabled})));
  select.value = value;
  return el('div', {}, [el('label', {htmlFor: id, text: label}), select]);
}
function renderSetup() {
  const wrap = $('#setup-content'); wrap.replaceChildren();
  $('#setup-title').textContent = setupSteps[setupStep];
  $('#setup-count').textContent = `Step ${setupStep + 1} of ${setupSteps.length}`;
  const steps = $('#setup-steps'); steps.replaceChildren(...setupSteps.map((name, index) =>
    el('li', {class: index === setupStep ? 'active' : '', text: `${index + 1}. ${name}`, 'aria-current': index === setupStep ? 'step' : 'false'})));
  const p = setupDraft.profile, prefs = setupDraft.preferences;
  if (setupStep === 0) {
    wrap.append(el('p', {text: 'Upload your PDF or DOCX (up to 1 MB), or enter everything manually. Import never replaces your saved profile until you finish setup.'}));
    const file = el('input', {type: 'file', accept: '.pdf,.docx', id: 'setup-file'});
    wrap.append(el('label', {htmlFor: 'setup-file', text: 'Your resume'}), file,
      setupButton('Extract resume text', async () => {
        const selected = file.files[0];
        if (!selected) throw new Error('Choose your PDF or DOCX first.');
        if (selected.size > 1000000) throw new Error('Choose a resume up to 1 MB.');
        const bytes = new Uint8Array(await selected.arrayBuffer());
        let binary = ''; for (const byte of bytes) binary += String.fromCharCode(byte);
        const result = await api('setup/extract', {name: selected.name, data: btoa(binary)});
        setupDraft.text = result.text;
        setupDraft.extractedLinks = result.links || [];
        applySetupImport(result);
        setupDraft.structured = false; setupDraft.profileTouched = false;
        setupChanged(); renderSetup(); notify(result.notice);
      }), setupField('Extracted resume text (editable; optional for manual entry)', setupDraft.text,
        v => {setupDraft.text = v; setupDraft.structured = false;}, {area: true}));
    wrap.append(el('p', {class: 'hint', text: 'Text extraction happens locally. After you choose an AI, “Fill profile from resume” sends this text to that provider and fills the editable review form. Scanned PDFs need a text version or manual entry.'}));
    if (setupDraft.extractedLinks) {
      const links = el('details', {open: true}, [el('summary', {text: `Extracted hyperlink targets (${setupDraft.extractedLinks.length})`})]);
      if (!setupDraft.extractedLinks.length) links.append(el('p', {text: 'No readable web links were recovered. Add missing URLs in the profile review step.'}));
      for (const url of setupDraft.extractedLinks) links.append(el('p', {}, [el('a', {href: url, text: url, target: '_blank', rel: 'noopener noreferrer'})]));
      wrap.append(links);
    }
  } else if (setupStep === 1) {
    const choices = [['rules', 'No AI — use original wording and text rules'], ['auto', 'Automatic fallback across configured providers']];
    for (const agent of state.setup.agents.filter(a => a.supported)) choices.push([agent.id, `${agent.id === 'claude' ? 'Claude Code' : 'Codex'} — ${agent.installed ? 'installed; test login below' : 'not installed'}`, !agent.installed]);
    for (const provider of state.credentials.providers.filter(a => a.id !== 'zai')) choices.push([provider.id, provider.name + ' API key']);
    wrap.append(setupSelect('AI for resume tailoring and job review', choices, prefs.provider, v => {prefs.provider = v; renderSetup();}));
    wrap.append(el('p', {class: 'hint', text: 'Installed does not mean signed in. A connection test uses a tiny prompt without your resume and may consume provider quota. Selecting one provider prevents fallback to others. Automatic mode may send data to other configured providers.'}));
    const external = (text, href) => el('a', {text, href, target: '_blank', rel: 'noopener noreferrer'});
    wrap.append(el('p', {}, [external('Install Codex', 'https://developers.openai.com/codex/cli/'), document.createTextNode(' · '), external('Install Claude Code', 'https://code.claude.com/docs/en/setup')]));
    wrap.append(el('p', {}, [external('Free-tier Gemini API: get a key', 'https://aistudio.google.com/apikey'), document.createTextNode(' · '), external('Current free quotas', 'https://ai.google.dev/gemini-api/docs/rate-limits')]));
    wrap.append(el('p', {class: 'hint', text: 'Free quotas depend on account, model and availability. A paid subscription is not required for rules mode. Gemini CLI has a free daily allowance, but its CLI and Antigravity are detection-only in this version; choose Gemini API here for the supported free-tier integration.'}));
    for (const agent of state.setup.agents.filter(a => !a.supported)) wrap.append(el('p', {class: 'hint', text: `${agent.id}: ${agent.installed ? 'detected' : 'not detected'} — direct integration unavailable.`}));
    if (['openai', 'gemini', 'anthropic', 'openrouter'].includes(prefs.provider)) {
      const provider = state.credentials.providers.find(a => a.id === prefs.provider);
      const keys = state.credentials.keys.filter(k => k.provider === prefs.provider);
      wrap.append(el('p', {text: keys.length ? `${keys.length} saved key(s) available. Manage them later in Settings.` : 'Add a key to use this provider.'}));
      const keyId = 'setup-key', modelId = 'setup-model';
      const key = el('input', {id: keyId, type: 'password', autocomplete: 'off', placeholder: 'API key'});
      const model = el('input', {id: modelId, value: keys[0]?.model || provider.default_model});
      wrap.append(el('label', {htmlFor: keyId, text: 'API key (stored only on this computer)'}), key,
        el('label', {htmlFor: modelId, text: 'Provider model ID'}), model,
        setupButton('Save API key', async () => {
          state.credentials = await api('keys/add', {provider: prefs.provider, key: key.value, model: model.value});
          key.value = ''; renderKeys(); renderSetup(); notify('Key saved locally.');
        }));
    }
    if (setupDraft.text && prefs.provider !== 'rules') wrap.append(el('p', {class: 'hint', text: 'Continuing with “Fill profile from resume” sends the extracted resume text to your selected AI and fills experience, education, projects, skills and links. You will review and edit every field before saving.'}));
    if (prefs.provider !== 'rules') wrap.append(setupButton('Test selected AI connection', async () => {
      await api('setup/test-ai', {provider: prefs.provider}); notify('Selected AI connection is ready.');
    }));
    wrap.append(setupButton('Rescan installed tools', async () => {state = await api('state'); renderSetup();}));
  } else if (setupStep === 2) {
    wrap.append(el('p', {text: 'Confirm every extracted field. Remove anything incorrect. Leave experience empty if you are a fresher. Missing facts must be entered by you; AI must not invent bullets.'}));
    if (setupDraft.text) wrap.append(setupButton('Refill profile from resume (replaces this draft)', async () => {
      if (prefs.provider === 'rules') throw new Error('Choose an AI first, or enter your profile manually below.');
      const result = await api('setup/structure', {text: setupDraft.text, provider: prefs.provider});
      applySetupImport(result); setupDraft.structured = true; setupDraft.profileTouched = false; setupChanged(); renderSetup();
      notify('Review all extracted details, dates, links and bullet claims before continuing.');
    }));
    wrap.append(el('div', {id: 'setup-missing', class: 'report', role: 'status', 'aria-live': 'polite'}));
    if (!p.experience.length) {
      const fresher = el('input', {type: 'checkbox', checked: !!prefs.no_experience, id: 'setup-fresher', onchange: e => {
        prefs.no_experience = e.target.checked; if (e.target.checked) prefs.years = 0;
        setupChanged(); updateSetupMissing();
      }});
      wrap.append(el('label', {class: 'setup-confirm', htmlFor: 'setup-fresher'}, [fresher, el('span', {text: 'I am a fresher and have no professional experience to list.'})]));
    }
    const basics = el('div', {class: 'form-grid'});
    for (const key of ['name', 'email', 'phone']) basics.append(setupField(key[0].toUpperCase() + key.slice(1), p.basics[key], v => p.basics[key] = v));
    for (const [label, host] of [['GitHub', 'github.com'], ['LinkedIn', 'linkedin.com']]) {
      basics.append(setupField(label + ' profile URL (optional if unavailable)', socialLink(p, host)?.url || '', v => {
        p.basics.links = p.basics.links.filter(link => !isSocialHost(link.url, host));
        if (v.trim()) p.basics.links.push({label, url: v.trim()});
      }));
    }
    renderSetupLinks(basics, p.basics, 'links', true);
    wrap.append(el('article', {class: 'panel'}, [el('h3', {text: 'Contact details'}), basics]));
    const definitions = {
      experience: ['Experience', ['org', 'role', 'location', 'start', 'end'], () => ({id: crypto.randomUUID(), org: '', role: '', location: '', start: '', end: '', bullets: [], stack: [], max_bullets: 3})],
      education: ['Education', ['org', 'degree', 'start', 'end', 'detail'], () => ({org: '', degree: '', start: '', end: '', detail: ''})],
      coding_profiles: ['Coding profiles / achievements', ['text'], () => ({text: ''})],
      projects: ['Projects — at least 3 confirmed bullets each', ['name'], () => ({id: crypto.randomUUID(), name: '', angles: [], stack: [], bullets: [1,2,3].map(() => ({id: crypto.randomUUID(), text: '', status: 'shipped', angles: []}))})]
    };
    for (const [section, [title, keys, fresh]] of Object.entries(definitions)) {
      const article = el('article', {class: 'panel'}, [el('h3', {text: title})]);
      p[section].forEach((entry, index) => {
        const row = el('div', {class: 'form-grid entry'});
        for (const key of keys) row.append(setupField(({org: 'Organization', start: 'Start date (e.g. Jan 2024)', end: 'End date (or Present)', text: 'Description'})[key] || key, entry[key], v => entry[key] = v));
        if (section === 'experience' || section === 'projects') row.append(bulletEditor(entry, section === 'projects'));
        if (section === 'projects') {
          row.append(setupField('Technologies (comma separated)', entry.stack.join(', '), v => entry.stack = commaList(v)), focusEditor(entry));
          renderSetupLinks(row, entry, 'link');
        }
        if (section === 'coding_profiles') renderSetupLinks(row, entry, 'link');
        row.append(setupButton('Remove entry', async () => {p[section].splice(index, 1); setupChanged(); renderSetup();}));
        article.append(row);
      });
      article.append(setupButton('Add ' + title.split(' —')[0], async () => {p[section].push(fresh()); setupChanged(); renderSetup();}));
      wrap.append(article);
    }
    const skills = el('article', {class: 'panel'}, [el('h3', {text: 'Skills'})]);
    for (const [group, items] of Object.entries(p.skills)) {
      if (!Array.isArray(items) || group.startsWith('_')) continue;
      skills.append(setupField(group + ' (comma separated)', items.join(', '), v => p.skills[group] = commaList(v)), setupButton('Remove ' + group, async () => {delete p.skills[group]; setupChanged(); renderSetup();}));
    }
    const group = el('input', {'aria-label': 'New skill group', placeholder: 'New group, e.g. Languages'});
    skills.append(group, setupButton('Add skill group', async () => {
      const name = group.value.trim(); if (!name || name.startsWith('_') || Object.hasOwn(p.skills, name)) throw new Error('Enter a unique skill group name.');
      Object.defineProperty(p.skills, name, {value: [], writable: true, enumerable: true, configurable: true}); setupChanged(); renderSetup();
    })); wrap.append(skills);
  } else if (setupStep === 3) {
    wrap.append(el('p', {text: 'Set your search preferences explicitly. Experience may differ from the dates in your resume; enter 0 if you are a fresher. These filters apply to the companies selected below.'}));
    const grid = el('div', {class: 'form-grid'});
    grid.append(setupField('Professional experience (years — confirm the suggestion from resume dates)', prefs.years, v => {prefs.years = v; setupDraft.yearsEdited = true;}),
      setupField('Locations (comma separated; e.g. India, Remote)', prefs.locations, v => prefs.locations = v),
      setupField('Target job titles / keywords (comma separated)', prefs.include, v => prefs.include = v),
      setupField('Exclude titles / keywords (optional)', prefs.exclude, v => prefs.exclude = v),
      setupField('PDF filename (without .pdf)', setupDraft.settings.filename, v => setupDraft.settings.filename = v),
      setupField('Output folder (absolute path)', setupDraft.settings.output_dir, v => setupDraft.settings.output_dir = v));
    wrap.append(grid, setupButton('Choose output folder', async () => {
      const result = await api('folder', {initial: setupDraft.settings.output_dir});
      if (result.path) {setupDraft.settings.output_dir = result.path; setupChanged(); renderSetup();}
    }));
    const cap = p._locked?.max_projects_on_page || 2;
    wrap.append(setupSelect('Projects on each resume', [1,2,3,4,5].map(n => [String(n), String(n)]), String(cap), v => {p._locked ||= {}; p._locked.max_projects_on_page = Number(v);}));
    const details = el('details', {}, [el('summary', {text: `Companies (${setupDraft.companies.length}) — review, add or remove`})]);
    details.append(el('p', {class: 'hint', text: 'Removing an existing company also removes its saved jobs when setup is completed. You can also manage companies later in Jobs → Companies.'}));
    setupDraft.companies.forEach((company, index) => {
      details.append(el('div', {class: 'setup-company'}, [el('span', {text: company.name}), el('a', {href: company.url, text: 'Careers ↗', target: '_blank', rel: 'noopener'}),
        setupButton('Remove', async () => {setupDraft.companies.splice(index, 1); setupChanged(); renderSetup();})]));
    });
    const name = el('input', {placeholder: 'Company name', 'aria-label': 'Company name'}), url = el('input', {placeholder: 'https://company.com/careers', 'aria-label': 'Careers URL'});
    details.append(name, url, setupButton('Add company', async () => {
      if (!name.value.trim() || !safeUrl(url.value) || !/^https?:\/\//i.test(url.value)) throw new Error('Enter a company name and full careers URL.');
      setupDraft.companies.push({name: name.value.trim(), url: url.value.trim()}); setupChanged(); renderSetup();
    })); wrap.append(details);
  } else {
    wrap.append(el('p', {text: 'Build your baseline PDF, check its contents, then confirm. The same one-page checks remain active for every future export. Additional projects stay in your library.'}));
    wrap.append(el('p', {text: `${p.basics.name || 'Name missing'} · ${p.basics.email || 'Email missing'} · ${p.experience.length} experience entries · ${p.education.length} education entries · ${p.projects.length} projects · ${setupDraft.companies.length} companies`}),
      el('p', {text: `AI: ${prefs.provider} · Experience: ${prefs.years} years · Locations: ${prefs.locations} · Target roles: ${prefs.include || 'missing'}`}));
    const missing = Object.entries(state.dependencies).filter(([, ok]) => !ok).map(([name]) => name);
    wrap.append(el('p', {class: missing.length ? 'warning' : 'hint', text: missing.length ? 'Missing PDF tools: ' + missing.join(', ') + '. Install Typst and Poppler (macOS: brew install typst poppler).' : 'PDF tools detected. Build the actual PDF to check layout and text.'}));
    wrap.append(setupButton('Build one-page setup preview', async () => {
      const payload = setupPayload(); const signature = JSON.stringify(payload);
      const result = await api('setup/check', payload);
      setupReceipt = result.receipt; setupSignature = signature;
      const box = $('#setup-preview'); box.replaceChildren();
      if (result.notices.length) box.append(el('p', {class: 'warning', text: result.notices.join(' ')}));
      box.append(el('iframe', {title: 'Setup one-page resume preview', src: result.url}), el('a', {href: result.url, target: '_blank', rel: 'noopener', text: 'Open PDF ↗'}));
    }), el('div', {id: 'setup-preview'}));
    const confirm = el('input', {type: 'checkbox', id: 'setup-confirm'});
    wrap.append(el('label', {class: 'setup-confirm', htmlFor: 'setup-confirm'}, [confirm, el('span', {text: 'I reviewed my profile, experience, preferences, company changes and PDF. All claims are accurate.'})]),
      setupButton('Finish setup & open workspace', async () => {
        if (!confirm.checked) throw new Error('Review the PDF and confirm your information first.');
        if (!setupReceipt || setupSignature !== JSON.stringify(setupPayload())) throw new Error('Build a new PDF after your latest changes.');
        state = await api('setup/complete', {...setupPayload(), receipt: setupReceipt, confirmed: true});
        profile = state.profile; setupDraft = null;
        $('#setup-dialog').close(); location.reload();
      }, true));
  }
  const actions = $('#setup-actions'); actions.replaceChildren();
  if (setupStep > 0) actions.append(setupButton('Back', async () => {setupStep--; renderSetup();}));
  if (setupStep < setupSteps.length - 1) actions.append(setupButton(setupStep === 1 && setupDraft.text && prefs.provider !== 'rules' && !setupDraft.structured && !setupDraft.profileTouched ? 'Fill profile from resume →' : 'Continue →', continueSetup, true));
  if (!state.setup.required) actions.append(setupButton('Cancel setup', async () => {setupDraft = null; $('#setup-dialog').close();}));
  updateSetupMissing();
  $('#setup-content').scrollTop = 0;
}
function renderSetupLinks(container, owner, key, skipSocial = false) {
  const all = key === 'links' ? owner.links : owner.link ? [owner.link] : [];
  const links = skipSocial ? all.filter(link => !isSocialHost(link.url, 'github.com') && !isSocialHost(link.url, 'linkedin.com')) : all;
  for (const item of links) {
    container.append(setupField('Link label', item.label, v => item.label = v), setupField('Link URL', item.url, v => item.url = v),
      setupButton('Remove link', async () => {
        if (key === 'links') owner.links = owner.links.filter(x => x !== item); else delete owner.link;
        setupChanged(); renderSetup();
      }));
  }
  if (key === 'links' || !links.length) container.append(setupButton('Add link', async () => {
    if (key === 'links') owner.links.push({label: '', url: ''}); else owner.link = {label: '', url: ''};
    setupChanged(); renderSetup();
  }));
}

function isSocialHost(value, host) {
  try {return new URL(value).hostname.toLowerCase().replace(/^www\./, '') === host;} catch {return false;}
}
function socialLink(profile, host) {return profile.basics.links.find(link => isSocialHost(link.url, host));}
function applySetupImport(result) {
  setupDraft.profile = result.profile;
  if (!setupDraft.yearsEdited && result.review) setupDraft.preferences.years = result.review.experience_years;
  setupDraft.preferences.no_experience = false;
  setupDraft.importReview = result.review;
}
async function continueSetup() {
  let warning = null;
  if (setupStep === 1 && setupDraft.text && setupDraft.preferences.provider !== 'rules' && !setupDraft.structured && !setupDraft.profileTouched) {
    try {
      const result = await api('setup/structure', {text: setupDraft.text, provider: setupDraft.preferences.provider});
      applySetupImport(result); setupDraft.structured = true;
    } catch (error) {
      warning = error.message + ' Locally extracted fields are still shown below; fill any missing details manually.';
    }
  }
  if (setupStep === 2 && !setupDraft.profile.experience.length && !setupDraft.preferences.no_experience)
    throw new Error('No experience was found. Add your employment/internships, or confirm that you are a fresher.');
  setupStep++; setupChanged(); renderSetup();
  if (warning) notify(warning, true);
}
function updateSetupMissing() {
  const box = $('#setup-missing');
  if (!box || !setupDraft) return;
  const p = setupDraft.profile, missing = [];
  for (const key of ['name', 'email', 'phone']) if (!p.basics[key]?.trim()) missing.push('Add your ' + key + (key === 'phone' ? ' if applicable.' : '.'));
  for (const [name, host] of [['GitHub', 'github.com'], ['LinkedIn', 'linkedin.com']]) if (!socialLink(p, host)) missing.push(name + ' was not found. Add it below if available.');
  if (!p.experience.length && !setupDraft.preferences.no_experience) missing.push('Add experience or confirm you are a fresher.');
  for (const [section, keys] of [['experience', ['org', 'role', 'start', 'end']], ['education', ['org', 'degree', 'start', 'end']]]) {
    if (!p[section].length && section === 'education') missing.push('Add your education.');
    p[section].forEach((entry, i) => {const absent = keys.filter(key => !entry[key]?.trim()); if (absent.length) missing.push(`${section} ${i + 1}: confirm ${absent.join(', ')}.`);});
  }
  if (!Object.values(p.skills).some(items => Array.isArray(items) && items.length)) missing.push('Add your confirmed skills.');
  if (!p.projects.length) missing.push('Add your projects.');
  for (const project of p.projects) if (project.bullets.filter(b => (b.status || 'shipped') === 'shipped' && b.text.trim()).length < 3) missing.push((project.name || 'Project') + ': add at least 3 confirmed bullets.');
  box.replaceChildren(el('strong', {text: missing.length ? 'Please fill or confirm these details' : 'Resume fields are filled — review and edit them below'}), ...missing.map(message => el('p', {text: message})));
}

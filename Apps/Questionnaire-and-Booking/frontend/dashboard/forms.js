'use strict';

function renderFormsPage(view) {
  const clone = (value) => JSON.parse(JSON.stringify(value));
  let saved = clone(window.TVE_FORM_DEFAULTS), draft = clone(saved);
  let selected = 4, tab = 'questions', previewFlow = 'questionnaire', connected = false, busy = false, disposed = false;
  const controller = new AbortController();
  // Keep unsaved work when moving between dashboard pages; scope it by environment.
  const draftKey = state.mode || 'unknown';
  const pending = state.formDrafts && state.formDrafts[draftKey];
  const dirty = () => JSON.stringify(draft) !== JSON.stringify(saved);
  const beforeUnload = (event) => { if (dirty()) { event.preventDefault(); event.returnValue = ''; } };
  window.addEventListener('beforeunload', beforeUnload);
  state.formsCleanup = () => {
    disposed = true; controller.abort(); window.removeEventListener('beforeunload', beforeUnload);
    state.formDrafts ||= {};
    state.formDrafts[state.mode || draftKey] = dirty() ? { draft: clone(draft), saved: clone(saved) } : null;
    state.formsCleanup = null;
  };
  const status = el('span', { class: 'forms-save-state', role: 'status', 'aria-live': 'polite', text: 'Connecting…' });
  const save = el('button', { class: 'btn btn-primary', disabled: true, onclick: publish }, icon('check'), 'Save changes');
  const reset = el('button', { class: 'btn btn-ghost', disabled: true, text: 'Discard changes', onclick: () => {
    draft = clone(saved); renderBody(); updateState();
  } });
  const notice = el('div', { class: 'forms-notice', role: 'status', hidden: true });
  const actions = el('div', { class: 'forms-actions' }, status, reset, save);
  const body = el('div');
  const count = el('span', { class: 'forms-small-pill', text: '14' });
  const questionsTab = el('button', { class: 'forms-tab active', role: 'tab', 'aria-selected': 'true', onclick: () => setTab('questions') }, icon('form'), 'Questionnaire', count);
  const brandingTab = el('button', { class: 'forms-tab', role: 'tab', 'aria-selected': 'false', onclick: () => setTab('branding') }, icon('image'), 'Branding');
  view.append(el('div', { class: 'forms-page' },
    el('div', { class: 'forms-heading' }, el('div', {}, el('div', { class: 'forms-eyebrow', text: 'APPLICANT EXPERIENCE' }), el('h1', { text: 'Forms' }), el('p', { text: 'A considered first impression, from enquiry to viewing.' })), actions),
    el('div', { class: 'forms-flow-strip' },
      el('div', { class: 'forms-flow-item' }, el('span', { class: 'forms-flow-icon' }, icon('form')), el('div', {}, el('strong', { text: 'Questionnaire' }), el('span', { text: 'Get to know your applicants' })), el('span', { class: 'forms-flow-tag', text: '14 questions' })),
      el('span', { class: 'forms-flow-arrow', text: '→', 'aria-hidden': 'true' }),
      el('div', { class: 'forms-flow-item' }, el('span', { class: 'forms-flow-icon booking' }, icon('calendar')), el('div', {}, el('strong', { text: 'Viewing booking' }), el('span', { text: 'Make the next step feel familiar' })), el('span', { class: 'forms-flow-tag', text: 'Shared branding' }))),
    notice,
    el('div', { class: 'forms-tabs', role: 'tablist', 'aria-label': 'Form settings' }, questionsTab, brandingTab), body,
    el('div', { class: 'forms-footer' }, icon('lock'), el('span', { text: 'Your changes apply to newly opened forms. Existing applicant responses stay intact.' }))));

  function updateState() {
    save.disabled = !connected || busy || !dirty(); reset.disabled = busy || !dirty();
    status.textContent = busy ? 'Saving…' : dirty() ? 'Unsaved changes' : connected ? 'All changes saved' : 'Preview mode';
    status.classList.toggle('is-dirty', dirty());
    count.textContent = String(draft.questions.length);
  }
  function message(text, error = false) { notice.hidden = !text; notice.textContent = text; notice.classList.toggle('is-error', error); }
  function setTab(value) {
    tab = value;
    questionsTab.classList.toggle('active', tab === 'questions'); brandingTab.classList.toggle('active', tab === 'branding');
    questionsTab.setAttribute('aria-selected', String(tab === 'questions')); brandingTab.setAttribute('aria-selected', String(tab === 'branding'));
    renderBody();
  }
  function change() { updateState(); renderPreview(); }
  let preview;
  function renderBody() {
    body.replaceChildren();
    preview = el('aside', { class: 'forms-preview', 'aria-label': 'Applicant preview' });
    if (tab === 'questions') {
      const list = el('nav', { class: 'forms-question-list', 'aria-label': 'Questionnaire questions' });
      let previousSection = null;
      draft.questions.forEach((q, index) => {
        const shortLabels = { guest_name: 'Full name', guest_email: 'Email address', guest_phone: 'Phone number', guest_property_address: 'Property', move_timing: 'Moving date', monthly_budget: 'Monthly budget', rental_duration: 'Rental duration', household_income: 'Household income', income_proof: 'Proof of income', occupants: 'Occupants', pets: 'Pets', smoke_vape: 'Smoking', adverse_credit: 'Credit history', booking_opt_in: 'Viewing preference' };
        const section = q.identityField ? 'APPLICANT DETAILS' : 'RENTAL PREFERENCES';
        if (section !== previousSection) list.append(el('div', { class: 'forms-list-caption', text: `${String(index + 1).padStart(2, '0')} / ${section}` }));
        previousSection = section;
        list.append(el('button', { class: `forms-question-item${selected === index ? ' selected' : ''}`, 'aria-current': selected === index ? 'true' : null, onclick: () => { selected = index; renderBody(); } },
          el('span', { class: 'forms-question-number', text: String(index + 1).padStart(2, '0') }), el('span', { text: shortLabels[q.id] }), el('span', { class: 'forms-question-chevron', text: '›' })));
      });
      body.append(el('div', { class: 'forms-workbench' }, list, questionEditor(), preview));
    } else {
      body.append(el('div', { class: 'forms-brand-layout' }, brandingEditor(), preview));
    }
    renderPreview();
  }
  function field(label, value, onInput, options = {}) {
    const input = el(options.multiline ? 'textarea' : 'input', { class: 'input forms-input', value: options.multiline ? null : value, type: options.multiline ? null : 'text', rows: options.multiline ? 3 : null, maxlength: options.max || 500, placeholder: options.placeholder || '', disabled: busy,
      oninput: (event) => { onInput(event.target.value); change(); } });
    if (options.multiline) input.value = value;
    return el('label', { class: 'forms-field' }, el('span', { class: 'forms-field-label', text: label }), input);
  }
  function questionEditor() {
    const q = draft.questions[selected];
    const types = { radio: 'Single choice', stepper: 'Amount', number: 'Number', name: 'Full name', email: 'Email', tel: 'Phone number', select: 'Property selection' };
    const editor = el('section', { class: 'forms-editor' },
      el('div', { class: 'forms-editor-top' }, el('span', { class: 'forms-eyebrow', text: `QUESTION ${String(selected + 1).padStart(2, '0')}` }),
        el('div', { class: 'forms-order-actions' },
          el('button', { class: 'forms-move-button', type: 'button', disabled: busy || selected === 0, 'aria-label': 'Move question up', onclick: () => moveQuestion(-1), text: 'Move up' }),
          el('span', { class: 'forms-required', text: q.required ? 'Required' : 'Optional' }),
          el('button', { class: 'forms-move-button', type: 'button', disabled: busy || selected === draft.questions.length - 1, 'aria-label': 'Move question down', onclick: () => moveQuestion(1), text: 'Move down' }))),
      el('h2', { text: 'Make it sound like you' }), el('p', { class: 'forms-muted', text: 'Keep questions clear, helpful and easy to answer.' }),
      field('Question', q.label, (value) => q.label = value, { multiline: true, max: 300 }),
      field('Help text', q.subtitle || '', (value) => q.subtitle = value, { placeholder: 'Add a little context (optional)' }),
      el('div', { class: 'forms-type-row' }, el('span', { text: 'Answer type' }), el('span', { class: 'forms-type-badge' }, icon(q.type === 'radio' ? 'circle' : 'form'), types[q.type] || q.type)));
    if (q.options) {
      editor.append(el('div', { class: 'forms-field-label forms-options-title', text: 'Answer choices' }));
      q.options.forEach((option, index) => editor.append(el('div', { class: 'forms-option-editor' }, el('span', { class: 'forms-option-dot', 'aria-hidden': 'true' }),
        field(`Choice ${index + 1}`, option.label, (value) => option.label = value, { max: 150 }))));
    } else if (['number', 'email'].includes(q.type)) editor.append(field('Placeholder', q.placeholder || '', (value) => q.placeholder = value));
    editor.append(el('div', { class: 'forms-editor-note' }, icon('lock'), el('span', { text: q.identityField ? 'Used to identify applicants who open the questionnaire without a personal invitation.' : 'Answer types and required fields support your qualification workflow.' })));
    return editor;
  }
  function moveQuestion(offset) {
    const nextIndex = selected + offset;
    if (nextIndex < 0 || nextIndex >= draft.questions.length || busy) return;
    const [question] = draft.questions.splice(selected, 1);
    draft.questions.splice(nextIndex, 0, question);
    selected = nextIndex;
    renderBody(); updateState();
  }
  function brandingEditor() {
    const upload = el('input', { type: 'file', accept: 'image/png,image/jpeg,image/webp', 'aria-label': 'Upload company logo', hidden: true, onchange: (event) => uploadLogo(event.target.files[0]) });
    const zone = el('button', { class: 'forms-upload', disabled: busy, onclick: () => upload.click() },
      el('span', { class: 'forms-upload-icon' }, icon('upload')), el('strong', { text: 'Upload your company logo' }), el('span', { text: 'Choose a PNG, JPG or WebP · up to 250 KB' }));
    const content = el('section', { class: 'forms-editor forms-brand-editor' }, el('div', { class: 'forms-eyebrow', text: 'ONE BRAND. EVERY STEP.' }),
      el('h2', { text: 'A familiar face throughout' }), el('p', { class: 'forms-muted', text: 'Your logo appears at the top of both the questionnaire and viewing booking experience.' }),
      upload, zone, el('div', { class: 'forms-or', text: 'or use an image link' }),
      field('Logo URL', draft.logo_url.startsWith('data:') ? '' : draft.logo_url, (value) => { draft.logo_url = value.trim(); }, { max: 2048, placeholder: 'https://your-website.com/logo.png' }),
      draft.logo_url.startsWith('data:') ? el('p', { class: 'forms-uploaded', text: '✓ Uploaded image ready to save' }) : null,
      el('div', { class: 'forms-brand-tips' }, el('strong', { text: 'Give your logo room to breathe' }), el('p', { text: 'A transparent background works best. Use a crisp image with a little space around the edges.' })),
      el('div', { class: 'forms-applies' }, icon('check'), 'Questionnaire', icon('check'), 'Viewing booking'),
      el('button', { class: 'btn btn-ghost', disabled: busy || !draft.logo_url, text: 'Use original logo', onclick: () => { draft.logo_url = ''; renderBody(); updateState(); } }));
    return content;
  }
  async function uploadLogo(file) {
    if (!file) return;
    if (!['image/png', 'image/jpeg', 'image/webp'].includes(file.type) || file.size > 250 * 1024) return message('Choose a PNG, JPG or WebP image smaller than 250 KB.', true);
    try {
      const data = await new Promise((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve(reader.result); reader.onerror = reject; reader.readAsDataURL(file); });
      await new Promise((resolve, reject) => { const image = new Image(); image.onload = resolve; image.onerror = reject; image.src = data; });
      if (disposed) return;
      draft.logo_url = data; message(''); renderBody(); updateState();
    } catch { message('This image could not be opened. Please choose another file.', true); }
  }
  function renderPreview() {
    if (!preview) return;
    const booking = tab === 'branding' && previewFlow === 'booking';
    const q = draft.questions[selected];
    const header = el('div', { class: 'forms-preview-heading' }, el('span', { class: 'forms-eyebrow', text: 'LIVE PREVIEW' }), el('span', { class: 'forms-preview-dot' }, 'Applicant view'));
    const logo = el('img', { class: 'forms-preview-logo', src: /^(https:\/\/|data:image\/(png|jpeg|webp);base64,)/.test(draft.logo_url) ? draft.logo_url : 'assets/tve-logo.svg', alt: 'Company logo', onerror: (event) => { event.target.hidden = true; } });
    const card = el('div', { class: 'forms-preview-paper' }, el('div', { class: 'forms-preview-brand' }, logo, el('span', { text: 'Thames Valley Estates' })),
      el('div', { class: 'forms-preview-content' }, el('div', { class: 'forms-preview-kicker', text: booking ? 'YOUR NEXT CHAPTER' : 'LET’S FIND YOUR NEXT HOME' }),
        el('h3', { text: booking ? 'Book a viewing' : 'A little about you' }), el('p', { text: booking ? 'Find a time to see your future home.' : 'Help us find the right place for you.' })));
    const content = card.querySelector('.forms-preview-content');
    if (booking) {
      content.append(el('div', { class: 'forms-preview-divider' }), el('strong', { text: 'Choose a viewing time' }), el('div', { class: 'forms-calendar-caption', text: 'October 2026' }),
        el('div', { class: 'forms-preview-days' }, ['M','T','W','T','F','S','S',12,13,14,15,16,17,18].map((d,i) => el('span', { class: i === 9 ? 'chosen' : '', text: d }))),
        el('div', { class: 'forms-time-slots' }, ['10:00', '11:30', '14:00'].map((time,i) => el('span', { class: i === 1 ? 'chosen' : '', text: time }))));
    } else {
      content.append(el('div', { class: 'forms-preview-progress' }, el('span', { style: `width:${(selected + 1) / draft.questions.length * 100}%` })),
        el('div', { class: 'forms-preview-step', text: `Question ${selected + 1} of ${draft.questions.length}` }),
        el('h4', { text: (q.label || 'Your question') + ' *' }), q.subtitle ? el('p', { class: 'forms-preview-help', text: q.subtitle }) : null);
      if (q.options) content.append(el('div', { class: 'forms-preview-options' }, q.options.map((option) => el('div', { class: 'forms-preview-option' }, el('span', { class: 'forms-radio' }), el('span', { text: option.label || 'Answer choice' })))));
      else if (q.type === 'stepper') content.append(el('div', { class: 'forms-preview-amount', text: `£${q.start.toLocaleString('en-GB')}` }), el('div', { class: 'forms-faux-slider' }, el('span')));
      else content.append(el('div', { class: 'forms-preview-input', text: q.placeholder || ({ name: 'First name      Last name', tel: '+44   Phone number', select: 'Search for a property' }[q.type]) || 'Your answer' }));
    }
    content.append(el('div', { class: 'forms-preview-next', text: booking ? 'Continue →' : 'Next →' }));
    const switcher = tab === 'branding' ? el('div', { class: 'forms-preview-switch', 'aria-label': 'Preview workflow' }, ['questionnaire','booking'].map((flow) => el('button', { class: previewFlow === flow ? 'active' : '', 'aria-pressed': String(previewFlow === flow), text: flow === 'booking' ? 'Booking' : 'Questionnaire', onclick: () => { previewFlow = flow; renderPreview(); } }))) : null;
    preview.replaceChildren(header, switcher, card, el('p', { class: 'forms-preview-caption', text: 'A preview of your changes. Save when everything feels right.' }));
  }
  async function publish() {
    if (!connected || busy) return;
    if (draft.questions.some((q) => !q.label.trim() || (q.options || []).some((o) => !o.label.trim()))) return message('Add text to every question and answer choice before saving.', true);
    if (draft.logo_url && !/^(https:\/\/|data:image\/(png|jpeg|webp);base64,)/.test(draft.logo_url)) return message('Use an HTTPS image URL or upload a logo.', true);
    busy = true; updateState(); renderBody(); message('');
    try {
      const result = await api('forms', draft, { method: 'PUT', cache: 'no-store', signal: controller.signal });
      if (disposed) return;
      saved = clone(result); draft = clone(result);
      message(`Changes saved to ${state.mode === 'sandbox' ? 'Sandbox' : 'Production'}. Newly opened forms will use your updated questions and logo.`);
    } catch (error) { if (!disposed) message(`Could not save. ${error.message} Your edits are still here.`, true); }
    finally { busy = false; if (!disposed) { updateState(); renderBody(); } }
  }
  renderBody();
  api('forms', undefined, { cache: 'no-store', signal: controller.signal }).then((result) => {
    if (disposed) return;
    if (!Array.isArray(result.questions) || result.questions.length !== 14) throw new Error('The form settings response was unreadable.');
    const localEdits = dirty();
    saved = clone(result); if (!localEdits) draft = clone(result); connected = true;
    if (pending && !localEdits) { draft = clone(pending.draft); message('Your unsaved edits have been restored.'); }
    renderBody(); updateState();
  }).catch((error) => {
    if (disposed) return;
    if (pending) { draft = clone(pending.draft); saved = clone(pending.saved); renderBody(); }
    message('The forms service is unavailable. You can explore the editor, but saving requires a backend connection. Reload this page to retry.', true); updateState();
  });
}

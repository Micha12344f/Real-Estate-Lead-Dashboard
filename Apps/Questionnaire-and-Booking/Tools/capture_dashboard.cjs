// Requires Playwright and Microsoft Edge. All screenshots use live synthetic data.
const {chromium} = require(process.env.SANDBOX_PLAYWRIGHT_MODULE || 'playwright');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const output = path.join(__dirname, 'screenshots', 'dashboard');

(async () => {
  fs.mkdirSync(output, {recursive: true});
  const browser = await chromium.launch({channel: 'msedge', headless: true});
  try {
    const context = await browser.newContext({viewport: {width: 1440, height: 1050}, deviceScaleFactor: 1});
    const page = await context.newPage();
    const failures = [];
    page.on('pageerror', e => failures.push(e.message));
    page.on('response', r => {
      if (r.url().includes('/sandbox/api/') && r.status() >= 400)
        failures.push(`${r.status()} ${new URL(r.url()).pathname}`);
    });
    const captures = [];
    async function capture(name, fullPage = true) {
      await page.screenshot({path: path.join(output, name + '.png'), fullPage, animations: 'disabled', caret: 'hide'});
      captures.push(name);
    }
    async function route(name, title) {
      await page.keyboard.press('Escape');
      await page.goto('http://127.0.0.1:3002/#/' + name);
      await page.getByRole('heading', {name: title, exact: true}).waitFor();
      await page.waitForFunction(() => state.leadsLoaded && state.online === true);
      await page.waitForTimeout(350);
    }
    await route('overview', 'Overview');
    await page.waitForFunction(() => state.metrics !== null && state.funnel !== null);
    const leads = await page.evaluate(() => state.leads);
    assert(leads.length, 'Need live synthetic leads to document the dashboard');
    assert(leads.every(c => /@[^@]+\.(invalid|test)$/.test(c.email || '')),
      'Screenshot capture requires synthetic-only lead data');
    await capture('overview');
    fs.copyFileSync(path.join(output, 'overview.png'), path.join(__dirname, 'sandbox-dashboard.png'));

    await route('inbox', 'Inbox');
    await capture('inbox');
    await route('leads', 'Leads');
    await capture('leads');
    const chosen = leads.find(c => c.questionnaire && c.property_id && !c.viewing_slot_start) || leads[0];
    const row = page.locator('table.leads tbody tr').filter({has: page.locator('td.cell-email', {hasText: chosen.email})});
    await row.locator('.star-btn').click(); // Disposable browser's localStorage only.
    await row.locator('td.cell-name').click();
    await page.locator('#drawer[aria-hidden="false"] .drawer-name').waitFor();
    await page.locator('#drawer .drawer-section-title').filter({hasText: 'Questionnaire answers'}).waitFor();
    await page.waitForFunction(id => String(state.drawerLead?.id) === String(id) && state.drawerLead?.raw_email_body != null, chosen.id);
    assert.equal(await page.locator('#drawer .drawer-email').textContent(), chosen.email);
    await page.locator('#drawer .drawer-body').evaluate(node => { node.scrollTop = 0; });
    await capture('lead-details', false);
    await page.locator('#drawer .drawer-section-title').filter({hasText: 'Questionnaire answers'}).evaluate(node => node.scrollIntoView({block: 'start'}));
    await capture('lead-answers', false);
    await page.keyboard.press('Escape');
    await route('views', 'Views');
    await capture('views');
    // Verify a saved view really filters the table.
    await page.goto('http://127.0.0.1:3002/#/leads?view=high-intent');
    await page.getByRole('heading', {name: 'Leads', exact: true}).waitFor();
    await page.waitForFunction(() => state.leadsLoaded && state.online === true);
    assert(await page.getByText('View: High intent', {exact: true}).count());
    await route('myleads', 'My leads');
    assert(await page.locator('td.cell-email').filter({hasText: chosen.email}).count());
    await capture('my-leads');
    await route('properties', 'Properties');
    await page.waitForFunction(() => state.properties.length > 0);
    const property = await page.evaluate(id => state.properties.find(p => String(p.id) === String(id)),
      chosen.catalogue_property_id ?? chosen.property_id);
    assert(property, 'Chosen lead must resolve to the selectable catalogue');
    const propertyCard = page.locator('.property-card').filter({has: page.locator('.prop-address', {hasText: property.address})});
    const propertyLeadCount = leads.filter(l => String(l.catalogue_property_id ?? l.property_id) === String(property.id)).length;
    assert.equal(await propertyCard.locator('.prop-stats b').first().textContent(), String(propertyLeadCount));
    await propertyCard.scrollIntoViewIfNeeded();
    await capture('properties', false);
    await propertyCard.getByRole('button', {name: 'View leads', exact: true}).click();
    await page.waitForURL('**/#/leads?property=' + property.id);
    await page.getByRole('heading', {name: 'Leads', exact: true}).waitFor();
    assert.equal(await page.locator('table.leads tbody tr').count(), propertyLeadCount);
    assert(await page.locator('td.cell-email').filter({hasText: chosen.email}).count());

    await route('leads', 'Leads');
    const assignRow = page.locator('table.leads tbody tr').filter({has: page.locator('td.cell-email', {hasText: chosen.email})});
    await assignRow.locator('td.cell-name').click();
    await page.locator('#drawer[aria-hidden="false"] .drawer-name').waitFor();
    await page.locator('#drawer .drawer-actions').getByRole('button', {name: 'Assign property', exact: true}).click();
    await page.locator('#modal:not([hidden])').waitFor();
    await page.waitForFunction(() => document.querySelector('#modal select')?.value === String(state.drawerLead.catalogue_property_id ?? state.drawerLead.property_id));
    await capture('assign-property', false); // Do not submit: screenshot only.
    await page.keyboard.press('Escape');
    await page.keyboard.press('Escape');
    assert.deepEqual(failures, []);
    console.log(JSON.stringify({status: 'passed', liveLeads: leads.length, screenshots: captures,
      checks: ['All six pages rendered', 'Live analytics loaded', 'Lead drawer', 'Saved-view filtering',
        'Browser-local starring', 'Property counts and filtering', 'Assignment preselection', 'No browser/API errors']}, null, 2));
  } finally {
    await browser.close();
  }
})().catch(e => {console.error(e.message); process.exit(1);});

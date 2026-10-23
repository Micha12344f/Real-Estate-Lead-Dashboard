// Run with SANDBOX_PLAYWRIGHT_MODULE pointing at an installed playwright module.
const { chromium } = require(process.env.SANDBOX_PLAYWRIGHT_MODULE || 'playwright');
const path = require('node:path');
const fs = require('node:fs');
(async()=>{
 const browser=await chromium.launch({channel:'msedge',headless:true});
 const context=await browser.newContext({viewport:{width:1440,height:1050}});
 const dashboard=await context.newPage();const errors=[];
 dashboard.on('pageerror',e=>errors.push(e.message));
 await dashboard.goto('http://127.0.0.1:3002');
 await dashboard.locator('#scenario option').first().waitFor({state:'attached'});
 await dashboard.locator('#fixture').fill('1');
 const runResponse=dashboard.waitForResponse(r=>r.url().endsWith('/sandbox/api/runs')&&r.request().method()==='POST');
 await dashboard.locator('#send').click();
 const started=await (await runResponse).json();
 await dashboard.waitForFunction(async email => {
   const data=await (await fetch('/sandbox/api/runs')).json();
   return data.runs.some(c=>c.email===email&&c.links.questionnaire);
 },started.contactEmail,{timeout:45000});
 const contact=await dashboard.evaluate(async email=>{
   const data=await (await fetch('/sandbox/api/runs')).json();
   return data.runs.find(c=>c.email===email);
 },started.contactEmail);
 const contactName=(contact.first_name+' '+contact.last_name).trim();
 await dashboard.locator('#runs button').filter({hasText:contactName}).first().click();
 await dashboard.locator('#detail').getByText('Contact '+contact.id,{exact:true}).waitFor({timeout:15000});
 await dashboard.getByRole('link',{name:'Open questionnaire ↗'}).waitFor({timeout:45000});
 const qUrl=await dashboard.getByRole('link',{name:'Open questionnaire ↗'}).getAttribute('href');
 const page=await context.newPage();page.on('pageerror',e=>errors.push(e.message));
 await page.goto(qUrl);
 // Fill the real multi-step questionnaire, not its API.
 for(let i=0;i<14;i++){
  if(await page.locator('.qv-success h2').filter({hasText:/check your emails/i}).count())break;
  const radios=page.locator('#qv-question-area input[type=radio]');
  if(await radios.count()) await radios.first().check({force:true});
  const occupants=page.locator('input[data-field=occupants]');if(await occupants.count())await occupants.fill('2');
  const consent=page.locator('#qv-consent-check');if(await consent.isVisible())await consent.check();
  await page.locator('#qv-next-btn').click();
  await page.waitForTimeout(250);
 }
 await page.locator('.qv-success h2').filter({hasText:/check your emails/i}).waitFor({timeout:15000});
 // The form now tells the lead to check their emails; open the booking form from the dashboard.
 await dashboard.getByRole('link',{name:'Open booking form ↗'}).waitFor({timeout:45000});
 const bUrl=await dashboard.getByRole('link',{name:'Open booking form ↗'}).getAttribute('href');
 await page.goto(bUrl);
 await page.locator('.day-btn:not(:disabled)').first().waitFor({timeout:15000});
 await page.locator('.day-btn:not(:disabled)').first().click();
 await page.locator('.slot-btn:not(:disabled)').first().click();
 await page.locator('#form-submit').click();
 await page.locator('#step-success:not(.hidden)').waitFor({timeout:15000});
 // Re-opening the booked link must show the already-requested confirmation, not the calendar.
 await page.goto(bUrl);
 await page.locator('#step-success:not(.hidden)').waitFor({timeout:15000});
 await dashboard.waitForTimeout(3500);
 await dashboard.screenshot({path:path.join(__dirname,'sandbox-dashboard.png'),fullPage:true});
 await page.screenshot({path:path.join(__dirname,'sandbox-viewing-result.png'),fullPage:true});
 if(errors.length)throw new Error('Browser errors: '+errors.join('; '));
 const report={status:'passed',checks:['Dashboard interaction','Tokenised questionnaire page','Multi-step questionnaire submission','Check-your-emails confirmation','Open booking from dashboard','Calendar/slot selection','Durable viewing acknowledgement','Already-requested link reload'],screenshots:['sandbox-dashboard.png','sandbox-viewing-result.png']};
 fs.writeFileSync(path.join(__dirname,'last-browser-e2e-result.json'),JSON.stringify(report,null,2));
 console.log(JSON.stringify(report,null,2));
 await browser.close();
})().catch(e=>{console.error(e);process.exit(1)});

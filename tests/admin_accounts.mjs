import fs from 'node:fs';
import assert from 'node:assert/strict';
const {chromium}=await import(process.env.PLAYWRIGHT_MODULE || '/tmp/akari-browser/node_modules/playwright-core/index.mjs');
const fixture=JSON.parse(fs.readFileSync('/tmp/akari-admin-browser.json','utf8'));
const browser=await chromium.launch({executablePath:process.env.CHROMIUM_PATH || '/home/user/.cache/ms-playwright/chromium-1148/chrome-linux/chrome',headless:true,args:['--no-sandbox']});
try {
 const page=await browser.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.goto(`http://127.0.0.1:${fixture.port}`);
 await page.locator('#access-token').fill(fs.readFileSync(fixture.base+'/state/admin/access.token','utf8').trim());await page.locator('#login-form button').click();
 await page.locator('nav a[data-page="settings"]').click();await page.locator('#settings-nav a[href="#settings/agent"]').click();
 const input=page.locator('[data-key="agent_allowed_openids"]');
 assert.equal(await input.inputValue(),'');
 await page.locator('[data-account="Owner-001"]').click();
 assert.equal(await input.inputValue(),'Owner-001');
 await page.locator('#save').click();
 await page.locator('#save:disabled').waitFor({state:'attached'});
 let config=JSON.parse(fs.readFileSync(fixture.base+'/config.json','utf8'));assert.deepEqual(config.agent_allowed_openids,['Owner-001']);
 await page.locator('[data-account="Owner-001"]').click();
 await input.fill('Guest-002\nManual_003\nGuest-002');await page.locator('#save').click();
 await page.locator('#save:disabled').waitFor({state:'attached'});
 config=JSON.parse(fs.readFileSync(fixture.base+'/config.json','utf8'));assert.deepEqual(config.agent_allowed_openids,['Guest-002','Manual_003']);
 await page.reload();await page.locator('[data-key="agent_allowed_openids"]').waitFor();
 assert.equal(await page.locator('[data-key="agent_allowed_openids"]').inputValue(),'Guest-002\nManual_003');
 await page.setViewportSize({width:390,height:844});assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
 assert.deepEqual(errors,[]);console.log('PASS: recent account add/remove, manual IDs, deduplication, save/reload, mobile layout; no production edits.');
} finally {await browser.close();}

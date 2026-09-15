// Read-only smoke check against the installed panel. Never modifies bot config or sends QQ messages.
import fs from 'node:fs';
import assert from 'node:assert/strict';
const {chromium}=await import(process.env.PLAYWRIGHT_MODULE || 'playwright-core');
const browser=await chromium.launch({executablePath:process.env.CHROMIUM_PATH || chromium.executablePath(),headless:true,args:['--no-sandbox']});
const context=await browser.newContext({viewport:{width:1440,height:1100},reducedMotion:'reduce'});
const page=await context.newPage();
const errors=[];
page.on('pageerror',e=>errors.push(e.message));
try {
  await page.goto('http://127.0.0.1:18763');
  await page.locator('#access-token').fill(fs.readFileSync('state/admin/access.token','utf8').trim());
  await page.locator('#login-form button').click();
  await page.locator('#hero-status').filter({hasText:'QQ 已连接'}).waitFor();
  const status=await (await context.request.get('http://127.0.0.1:18763/api/status')).json();
  assert.equal(status.service.active,'active');assert.equal(status.telemetry,true);assert.equal(status.pending_restart,false);
  await page.screenshot({path:'docs/admin-screenshots/overview.png',fullPage:true,animations:'disabled'});
  await page.locator('nav a[data-page="settings"]').click();
  await page.locator('#settings-nav a[href="#settings/voice"]').click();
  await page.screenshot({path:'docs/admin-screenshots/settings.png',fullPage:true,animations:'disabled'});
  await page.locator('nav a[data-page="system"]').click();
  await page.locator('#diagnose').click();await page.locator('.diagnostic-row').first().waitFor();
  await page.screenshot({path:'docs/admin-screenshots/system.png',fullPage:true,animations:'disabled'});
  const diagnostics=await page.locator('#diagnostics').innerText();
  assert(diagnostics.includes('ComfyUI'));
  await page.setViewportSize({width:390,height:844});
  await page.locator('#menu-toggle').click();await page.locator('nav a[data-page="overview"]').click();
  await page.locator('#sidebar').waitFor({state:'hidden'});
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  await page.screenshot({path:'docs/admin-screenshots/mobile.png',fullPage:true,animations:'disabled'});
  for(const width of [320,768,1024]){
    await page.setViewportSize({width,height:900});
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),`${width}px overflow`);
  }
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({live:true,service:status.service.active,qq:status.runtime.gateway,pending_restart:status.pending_restart,mobile_overflow:false,js_errors:errors,screenshots:'docs/admin-screenshots/'},null,2));
  const session=await (await context.request.get('http://127.0.0.1:18763/api/session')).json();
  await context.request.post('http://127.0.0.1:18763/api/logout',{headers:{'X-CSRF-Token':session.csrf},data:{}});
} finally {await browser.close();}

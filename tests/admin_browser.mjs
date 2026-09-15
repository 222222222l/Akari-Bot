// npm install && npx playwright-core install chromium; see docs/development.md
import fs from 'node:fs';
import assert from 'node:assert/strict';
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright-core');
const fixture = JSON.parse(fs.readFileSync('/tmp/akari-admin-browser.json','utf8'));
const token = fs.readFileSync(fixture.base+'/state/admin/access.token','utf8').trim();
const browser = await chromium.launch({executablePath:process.env.CHROMIUM_PATH || chromium.executablePath(),headless:true,args:['--no-sandbox']});
const context = await browser.newContext({viewport:{width:1440,height:1080},deviceScaleFactor:1});
const page = await context.newPage();
const errors=[];
page.on('pageerror',e=>errors.push(e.message));
page.on('console',m=>{if(m.type()==='error' && !m.text().includes('status of 401') && !m.text().includes('status of 409'))errors.push(m.text());});
const url=`http://127.0.0.1:${fixture.port}`;
let checks=0;
async function check(name,fn){await fn();checks++;console.log('PASS '+name);}
try {
  await page.goto(url);
  await check('login rejects an invalid token',async()=>{
    await page.locator('#access-token').fill('incorrect');await page.locator('#login-form button').click();
    await page.locator('#login-error').filter({hasText:'不正确'}).waitFor();
  });
  await check('login loads real API state and no secrets',async()=>{
    await page.locator('#access-token').fill(token);await page.locator('#login-form button').click();
    await page.locator('#hero-status').filter({hasText:'QQ 已连接'}).waitFor();
    assert(!await page.locator('body').innerText().then(t=>t.includes('fixture-secret-hidden')));
    assert.equal(await page.locator('.stat-card').count(),4);
  });
  await page.screenshot({path:'/tmp/akari-admin-desktop.png',fullPage:true,animations:'disabled'});
  await check('feature edits persist across navigation and save',async()=>{
    await page.locator('nav a[data-page="features"]').click();
    const initialMusic=await page.locator('#all-features input[data-key="music_enabled"]').isChecked();
    await page.locator('#all-features input[data-key="music_enabled"]').setChecked(!initialMusic);
    await page.locator('#save-bar').waitFor({state:'visible'});
    await page.locator('nav a[data-page="settings"]').click();
    await page.locator('#field-bot-temperature').fill('0.72');
    await page.locator('#save').click();await page.locator('#save-bar').waitFor({state:'hidden'});
    const raw=JSON.parse(fs.readFileSync(fixture.base+'/config.json','utf8'));
    assert.equal(raw.temperature,.72);assert.equal(raw.music_enabled,!initialMusic);assert.equal(raw.app_secret,'fixture-secret-hidden');
    await page.locator('#pending-banner').waitFor({state:'visible'});
  });
  await check('invalid numeric edit remains unsaved',async()=>{
    await page.locator('#field-bot-temperature').fill('3');await page.locator('#save').click();
    await page.locator('#toast').filter({hasText:'有效值'}).waitFor();
    assert.equal(JSON.parse(fs.readFileSync(fixture.base+'/config.json','utf8')).temperature,.72);
    await page.locator('#discard').click();await page.locator('#confirm-submit').click();await page.locator('#save-bar').waitFor({state:'hidden'});
  });
  await check('concurrent config change is not overwritten',async()=>{
    await page.locator('#field-bot-temperature').fill('0.8');
    const raw=JSON.parse(fs.readFileSync(fixture.base+'/config.json','utf8'));raw.temperature=.6;fs.writeFileSync(fixture.base+'/config.json',JSON.stringify(raw));
    await page.locator('#save').click();await page.locator('#global-error').filter({hasText:'其他窗口'}).waitFor();
    assert.equal(JSON.parse(fs.readFileSync(fixture.base+'/config.json','utf8')).temperature,.6);
    await page.locator('#discard').click();await page.locator('#confirm-submit').click();await page.locator('#save-bar').waitFor({state:'hidden'});
  });
  await check('music settings save without losing bot config',async()=>{
    await page.locator('#settings-nav a[href="#settings/voice"]').click();
    const steps=Number(await page.locator('#field-music-diffusion_steps').inputValue())===45?46:45;
    await page.locator('#field-music-diffusion_steps').fill(String(steps));await page.locator('#save').click();await page.locator('#save-bar').waitFor({state:'hidden'});
    assert.equal(JSON.parse(fs.readFileSync(fixture.base+'/music/config.json','utf8')).diffusion_steps,steps);
  });
  await check('logs retain stacks, hide secrets, filter and pause',async()=>{
    await page.locator('nav a[data-page="logs"]').click();
    await page.locator('#log-level').selectOption('error');
    await page.locator('#log-search').fill('browser-test');
    await page.waitForFunction(()=>document.querySelector('#log-content').innerText.includes('sample frame') && document.querySelectorAll('.log-entry').length===1);
    const content=await page.locator('#log-content').innerText();assert(content.includes('[已隐藏]'));assert(!content.includes('fixture-secret-hidden'));
    await page.locator('#log-pause').click();assert.equal(await page.locator('#log-live').innerText(),'已暂停');
    const downloadPromise=page.waitForEvent('download');await page.locator('#log-download').click();const download=await downloadPromise;assert(download.suggestedFilename().endsWith('.log'));
  });
  await page.screenshot({path:'/tmp/akari-admin-logs.png',fullPage:true,animations:'disabled'});
  await check('tasks can be filtered and inspected',async()=>{
    await page.locator('nav a[data-page="tasks"]').click();await page.locator('#task-filter').selectOption('failed');
    assert.equal(await page.locator('#task-list .task-row').count(),1);
    await page.locator('#task-list summary').click();assert((await page.locator('#task-list pre').innerText()).includes('ConnectionError'));
  });
  await check('diagnostics and explicit service confirmation work',async()=>{
    await page.locator('nav a[data-page="system"]').click();await page.locator('#diagnose').click();await page.locator('.diagnostic-row').first().waitFor();
    assert.equal(await page.locator('.diagnostic-row').count(),3);
    await page.locator('#page-system [data-action="restart"]').click();await page.locator('#confirm-dialog').waitFor({state:'visible'});
    await page.locator('#confirm-dialog button[value="cancel"]').click();
  });
  await check('mobile navigation and pages have no horizontal overflow',async()=>{
    await page.setViewportSize({width:390,height:844});
    for(const name of ['overview','features','settings','tasks','logs','system']){
      await page.locator('#menu-toggle').click();await page.locator(`nav a[data-page="${name}"]`).click();
      assert(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),name+' overflows');
    }
    await page.locator('#menu-toggle').click();await page.locator('nav a[data-page="overview"]').click();
    await page.locator('#sidebar').waitFor({state:'hidden'});
    await page.screenshot({path:'/tmp/akari-admin-mobile.png',fullPage:true,animations:'disabled'});
  });
  await check('logout invalidates authenticated access',async()=>{
    await page.locator('#menu-toggle').click();await page.locator('#logout').click();await page.locator('#login').waitFor({state:'visible'});
    assert.equal((await context.request.get(url+'/api/config')).status(),401);
  });
  assert.deepEqual(errors,[]);console.log(`Browser: ${checks} checks passed; no JavaScript/CSP errors.`);
} finally {await browser.close();}

const {chromium}=require('playwright');const assert=require('assert');
(async()=>{const browser=await chromium.launch({headless:true,executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});try{
 const page=await browser.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.goto(process.env.MIXCUT_TEST_URL+'/#settings');await page.getByText('本地服务已连接',{exact:false}).first().waitFor();
 assert.match(await page.locator('#selection-summary').textContent(),/4 个独立音乐文件夹/);
 await page.locator('[name=mode][value=single]').check();await page.locator('[name=count]').fill('4');await page.locator('[name=min_source_duration_minutes]').fill('0');
 const response=page.waitForResponse(r=>r.url().endsWith('/api/plan')&&r.request().method()==='POST');await page.locator('#plan-btn').click();const r=await response;assert.equal(r.status(),200);const batch=await r.json();assert.equal(batch.items.length,4);
 await page.locator('.music-folder-select').first().waitFor();assert.equal(await page.locator('.music-folder-select').count(),4);assert.equal(await page.locator('.music-folder-select').first().locator('option').count(),4);
 assert.deepEqual(errors,[]);console.log('V1.8.15 real browser passed: 4 leaf sets shown in summary and dropdowns; Generate Plan produces 4 isolated tasks.');
}finally{await browser.close()}})().catch(e=>{console.error(e);process.exit(1)});

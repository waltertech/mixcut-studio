const {chromium}=require('playwright');const assert=require('assert');
(async()=>{const browser=await chromium.launch({headless:true,executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});const page=await browser.newPage({viewport:{width:1400,height:1000}});const errors=[];page.on('pageerror',e=>errors.push(e.message));page.on('response',async r=>{if(r.status()>=400)console.error(await r.text());});try{
await page.goto(process.env.MIXCUT_TEST_URL+'/#settings');await page.getByText('本地服务已连接',{exact:false}).first().waitFor();
assert.equal(await page.locator('[name="include_track_titles"]').isChecked(),false);
await page.locator('[name="include_track_titles"]').check();await page.locator('[name="music_mode"][value="folder"]').check();
await page.locator('[name="count"]').fill('3');await page.locator('[name="min_duration"]').fill('10');await page.locator('[name="max_duration"]').fill('15');await page.locator('#plan-btn').click();await page.locator('.plan-item').first().waitFor();
assert.equal(await page.locator('#plan-items .track-title-option input:checked').count(),3);
await page.locator('#plan-items .track-title-option input').first().uncheck();await page.waitForFunction(()=>document.querySelectorAll('#plan-items .track-title-option input:checked').length===2);
await page.locator('#select-all-plan').click();await page.locator('#track-titles-disable-plan').click();await page.waitForFunction(()=>document.querySelectorAll('#plan-items .track-title-option input:checked').length===0);
await page.locator('#track-titles-enable-plan').click();await page.waitForFunction(()=>document.querySelectorAll('#plan-items .track-title-option input:checked').length===3);
await page.goto(process.env.MIXCUT_TEST_URL+'/#tasks');await page.locator('#batches .track-title-option').first().waitFor();await page.locator('#select-all-tasks').click();await page.locator('#track-titles-disable-tasks').click();await page.waitForFunction(()=>document.querySelectorAll('#batches .track-title-option input:checked').length===0);
assert.deepEqual(errors,[]);console.log('V1.5.5 real backend browser checks passed');
}finally{await browser.close();}})().catch(e=>{console.error(e);process.exit(1)});

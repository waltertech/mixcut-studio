const {chromium}=require('playwright');const assert=require('assert');
(async()=>{const browser=await chromium.launch({headless:true,executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});try{
 const page=await browser.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.goto(process.env.MIXCUT_TEST_URL+'/#settings');await page.getByText('本地服务已连接',{exact:false}).first().waitFor();
 assert.equal(await page.locator('#config-form [name="duration_basis"]').inputValue(),'music');
 assert.equal(await page.locator('#config-form [name="nonstop_music"]').inputValue(),'false');
 assert.equal(await page.locator('#config-form [name="min_duration"]').isVisible(),false);
 await page.locator('#config-form [name="music_mode"][value="folder"]').check();
 await page.locator('#plan-btn').click();await page.waitForFunction(()=>document.querySelector('#toast')?.textContent.includes('方案生成完成'));
 const drafts=await page.evaluate(async()=>await(await fetch('/api/batches')).json());
 const batch=drafts.find(b=>b.status==='draft');assert.equal(batch.config.duration_basis,'music');assert.equal(batch.config.nonstop_music,false);
 assert.equal(batch.items.length,3);assert.equal(batch.items[0].music.length,2);assert.ok(Math.abs(batch.items[0].duration-6)<.1);
 // Remove only the extra browser-created draft, keeping the API acceptance fixture.
 await page.evaluate(async id=>await fetch('/api/tasks/forget',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({selections:[{batch_id:id,item_ids:[]}]})}),batch.id);
 assert.deepEqual(errors,[]);console.log('Packaged music-led UI default and actual create-plan request passed: music duration, direct join, complete folder, hidden old duration controls.');
}finally{await browser.close();}})().catch(e=>{console.error(e);process.exit(1)});

const {chromium}=require('playwright');const assert=require('assert');
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
 const page=await browser.newPage({viewport:{width:1400,height:1100}});const errors=[];page.on('pageerror',e=>errors.push(e.message));
 try{
  await page.goto(process.env.MIXCUT_TEST_URL+'/#music-loop');await page.getByText('本地服务已连接',{exact:false}).first().waitFor();
  assert.equal(await page.locator('#music-loop').isVisible(),true);assert.equal(await page.locator('#settings').isVisible(),false);
  await page.locator('#loop-paths').fill(process.env.MIXCUT_LOOP_SET);await page.locator('#loop-output').fill(process.env.MIXCUT_LOOP_OUTPUT);
  await page.locator('#loop-import').click();await page.locator('.loop-task').first().waitFor();
  await page.waitForFunction(()=>document.querySelectorAll('.loop-task').length===3);
  await page.locator('#loop-parallel').selectOption('3');
  await page.locator('#loop-select').click();await page.locator('#loop-start').click();
  const peak=await page.evaluate(async()=>{let peak=0;for(let n=0;n<300;n++){const data=await(await fetch('/api/music-loop')).json();const count=data.tasks.filter(t=>['preparing','running'].includes(t.status)).length;peak=Math.max(peak,count);if(peak===3)break;await new Promise(r=>setTimeout(r,20));}return peak;});
  assert.equal(peak,3,'Three actual tasks must run concurrently');
  await page.waitForFunction(async()=>{const d=await(await fetch('/api/music-loop')).json();return d.tasks.length===3&&d.tasks.every(t=>t.status==='success');},{},{timeout:60000});
  const card=page.locator('.loop-task').first();await card.getByRole('button',{name:'查看与审核',exact:true}).waitFor({timeout:60000});
  await card.getByRole('button',{name:'查看与审核',exact:true}).click();await card.locator('video').waitFor();
  assert.equal(await card.locator('.keyframe').count(),0);
  await page.waitForTimeout(3400);assert.equal(await card.locator('.loop-details').isVisible(),true);
  await card.locator('video').evaluate(async v=>{v.muted=true;await v.play();});
  assert.equal(await card.locator('video').evaluate(v=>v.paused),false);
  await card.locator('.loop-details').getByRole('button',{name:'收起',exact:true}).click();
  assert.equal(await card.locator('video').evaluate(v=>v.paused),true);assert.equal(await card.locator('.loop-details').isVisible(),false);
  await card.getByRole('button',{name:'查看与审核',exact:true}).click();
  await card.getByRole('button',{name:'通过审核',exact:true}).click();
  await card.getByText('已审核，作品保留原处',{exact:true}).waitFor();assert.equal(await card.locator('.loop-details').isVisible(),false);
  await page.screenshot({path:process.env.MIXCUT_LOOP_SCREENSHOT,fullPage:true});
  await page.locator('#loop-delete').click();await page.waitForFunction(()=>document.querySelectorAll('.loop-task').length===0);
  await page.locator('a[data-view="settings"]').click();await page.locator('#settings').waitFor({state:'visible'});assert.equal(await page.locator('#settings').isVisible(),true);
  assert.deepEqual(errors,[]);console.log('Music-loop browser acceptance passed: import, three simultaneous renders with start-time parallel setting, no frame extraction, stable expansion, collapse pauses playback, approval, record deletion, original page navigation.');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1)});

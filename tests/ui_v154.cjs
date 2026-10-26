// Browser checks against a real isolated HTTP backend (URL supplied by the harness).
const {chromium}=require('playwright');
const assert=require('assert'),fs=require('fs'),path=require('path');
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
 const page=await browser.newPage({viewport:{width:1400,height:1000}}),errors=[];
 page.on('pageerror',e=>errors.push(e.message));
 try {
  await page.goto(process.env.MIXCUT_TEST_URL+'/#settings');
  await page.getByText('本地服务已连接',{exact:false}).first().waitFor();
  await page.locator('input[name="music_mode"][value="folder"]').check();
  assert.equal(await page.locator('.pool-settings').isVisible(),false);
  assert.equal(await page.locator('#folder-mode-help').isVisible(),true);
  await page.locator('input[name="count"]').fill('3');
  await page.locator('input[name="min_duration"]').fill('10');
  await page.locator('input[name="max_duration"]').fill('15');
  await page.locator('#plan-btn').click();
  await page.locator('.music-folder-select').first().waitFor();
  assert.equal(await page.locator('.plan-item').count(),3);
  const selections=await page.locator('.music-folder-select').evaluateAll(nodes=>nodes.map(n=>n.value));
  assert.equal(new Set(selections).size,3);
  assert.equal(await page.locator('.music-order li').count(),0,'long playlists must be lazy');
  assert.equal(await page.getByText(/本组使用 1 条任务/).count(),3);
  await page.getByRole('button',{name:'随机换文件夹',exact:true}).first().click();
  await page.waitForFunction(old=>document.querySelector('.music-folder-select').value!==old,selections[0]);
  const afterRandom=await page.locator('.music-folder-select').evaluateAll(nodes=>nodes.map(n=>n.value));
  assert.equal(new Set(afterRandom).size,3);
  await page.locator('.music-folder-select').first().selectOption(afterRandom[1]);
  await page.waitForFunction(()=>[...document.querySelectorAll('.music-track > p')].filter(p=>p.textContent.includes('本组使用 2 条任务')).length===2);
  const beforeShuffle=await page.locator('.music-folder-select').first().inputValue();
  await page.getByRole('button',{name:'重新排列音乐',exact:true}).first().click();
  await page.waitForTimeout(250);
  assert.equal(await page.locator('.music-folder-select').first().inputValue(),beforeShuffle);
  await page.getByText('展开歌曲顺序和播放次数',{exact:true}).first().click();
  await page.locator('.music-order li').first().waitFor();
  assert.ok(await page.getByText(/本条播放.*本组播放/).count()>0);
  await page.getByRole('button',{name:'随机替换这首歌',exact:true}).first().click();
  await page.waitForTimeout(250);
  assert.equal(await page.locator('.music-folder-select').first().inputValue(),beforeShuffle);
  assert.deepEqual(errors,[]);
  await page.screenshot({path:path.join(process.env.MIXCUT_TEST_ARTIFACTS,'folder-mode-ui.png'),fullPage:true});
  console.log(JSON.stringify({ok:true,tests:['real plan API','unique folder allocation','lazy playlist','random folder replacement','manual duplicate folder counts','same-folder shuffle','same-folder song replacement'],pageErrors:errors},null,2));
 } finally {await browser.close();}
})().catch(e=>{console.error(e.stack);process.exit(1)});

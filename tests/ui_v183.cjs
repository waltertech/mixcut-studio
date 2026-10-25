const {chromium}=require('playwright');const fs=require('fs');const path=require('path');const assert=require('assert');
(async()=>{const browser=await chromium.launch({headless:true,executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});try{
 const page=await browser.newPage();const requests=[];const errors=[];
 page.on('pageerror',e=>errors.push(e.message));page.on('request',r=>{if(r.url().endsWith('/api/scan/start'))requests.push(JSON.parse(r.postData()));});
 await page.goto(process.env.MIXCUT_TEST_URL+'/#assets');await page.getByText('本地服务已连接',{exact:false}).first().waitFor();
 const root=process.env.MIXCUT_SCAN_FIXTURE;
 const refresh=async()=>{const response=page.waitForResponse(r=>r.url().endsWith('/api/scan/start')&&r.request().method()==='POST');await page.locator('#scan-btn').click();const job=await(await response).json();await page.waitForFunction(async id=>{const s=await(await fetch('/api/scan/status?compact=1')).json();return s.id===id&&s.status==='completed';},job.id,{timeout:30000});await page.waitForFunction(()=>!document.querySelector('#scan-btn').disabled&&document.querySelector('#scan-progress').textContent.startsWith('扫描完成：'),{},{timeout:30000});return await page.evaluate(async()=>await(await fetch('/api/bootstrap')).json());};
 fs.unlinkSync(path.join(root,'videos','removed.mp4'));
 // Only the music path changes; video deletion at its unchanged path must still be found.
 await page.locator('#music-dir').fill(path.join(root,'new-music'));
 let boot=await refresh();assert.equal(boot.scan.videos.length,1);assert.equal(boot.scan.music.length,1);assert.equal(requests[0].kind,'both');
 fs.copyFileSync(path.join(root,'new-music','song.mp3'),path.join(root,'new-music','added.mp3'));
 fs.unlinkSync(path.join(root,'videos','kept.mp4'));
 boot=await refresh();assert.equal(boot.scan.videos.length,0);assert.equal(boot.scan.music.length,2);assert.equal(requests[1].kind,'both');
 fs.unlinkSync(path.join(root,'new-music','song.mp3'));fs.copyFileSync(path.join(root,'backup.mp4'),path.join(root,'videos','new.mp4'));
 boot=await refresh();assert.equal(boot.scan.videos.length,1);assert.equal(boot.scan.music.length,1);assert.ok(boot.scan.videos[0].path.endsWith('/new.mp4'));assert.ok(boot.scan.music[0].path.endsWith('/added.mp3'));
 assert.deepEqual(errors,[]);console.log('Packaged refresh UI passed: one changed path still refreshes both; repeated same-path clicks discover additions/deletions on both sides; no stale inventory or page errors.');
}finally{await browser.close();}})().catch(e=>{console.error(e);process.exit(1)});

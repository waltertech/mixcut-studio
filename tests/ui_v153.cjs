// Run with NODE_PATH pointing to a Playwright installation. Uses an isolated browser and mocked APIs.
const {chromium}=require('playwright');
const fs=require('fs'),path=require('path'),assert=require('assert');
(async()=>{
 const artifact=process.env.MIXCUT_TEST_ARTIFACTS || '/tmp/mixcut-v153-ui';fs.mkdirSync(artifact,{recursive:true});
 const media=fs.readFileSync(path.join(artifact,'sample.mp4'));
 const browser=await chromium.launch({headless:true,executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
 const page=await browser.newPage({viewport:{width:1400,height:1000}});
 const errors=[];page.on('pageerror',e=>errors.push(e.message));
 let item={id:'one',index:1,status:'success',progress:1,duration:65,output_name:'test.mp4',result:{output_size:100,output_mtime_ns:1,recovery_warnings:['片段已自动替换视频']},thumbnails:{status:'ready',total:3,version:'paired-v3'},music:[],segments:[]};
 let batch={id:'abc123',status:'completed',created_at:1,updated_at:1,output_folder:'/tmp/exports',items:[item]}, approvalRelease;
 const tiny=Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aZioAAAAASUVORK5CYII=','base64');
 const fulfill=(route,data)=>route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(data)});
 let frameRequests=0,rejected=false;
 await page.route('http://mixcut.test/**',async route=>{
  const u=new URL(route.request().url()),p=u.pathname;
  if(!p.startsWith('/api/')) {
   const file=p==='/'?'index.html':path.basename(p);return route.fulfill({status:200,contentType:file.endsWith('.js')?'application/javascript':file.endsWith('.css')?'text/css':'text/html',body:fs.readFileSync(path.join(process.cwd(),'static',file))});
  }
  if(p==='/api/bootstrap') return fulfill(route,{api_protocol:16,version:'1.6.0',review_dir:'/tmp/reviewed',scan:{videos:[],music:[],errors:[]},batches:[batch],sticker_catalog:{assets:[],templates:[]},ffmpeg_available:true});
  if(p==='/api/batches')return fulfill(route,{batches:batch.items.length?[batch]:[]});
  if(p==='/api/keyframes'){frameRequests++;return fulfill(route,{version:'paired-v3',total:3,frames:[0,30,60].map((time,index)=>({time,index}))});}
  if(p==='/api/keyframe')return route.fulfill({status:200,contentType:'image/png',body:tiny});
  if(p==='/api/output')return route.fulfill({status:200,contentType:'video/mp4',body:media});
  if(p==='/api/approve') {
   item.review={status:'copying'};batch.updated_at++;await new Promise(resolve=>approvalRelease=resolve);
   item.review={status:'approved',path:'/tmp/archive.mp4'};item.cleanup={output_deleted:false,output_error:'fixture cleanup failure'};batch.updated_at++;
   return fulfill(route,batch);
  }
  if(p==='/api/reject'){rejected=true;batch.items=[];return fulfill(route,{ok:true});}
  if(p==='/api/cache')return fulfill(route,{bytes:0,files:0});
  if(p==='/api/stickers')return fulfill(route,{assets:[],templates:[]});
  if(p==='/api/schedules')return fulfill(route,{schedules:[]});
  return fulfill(route,{});
 });
 try {
  await page.goto('http://mixcut.test/#tasks');await page.getByRole('button',{name:'查看与审核',exact:true}).waitFor();
  assert.equal(frameRequests,0,'collapsed rows should not fetch galleries');
  await page.getByRole('button',{name:'查看与审核',exact:true}).click();await page.locator('.keyframe').first().waitFor();
  assert.equal(await page.locator('.keyframe').count(),3);
  await page.getByRole('button',{name:'全选任务',exact:true}).click();
  assert.equal(await page.locator('.task-item.expanded').count(),1);assert.equal(await page.locator('.keyframe').count(),3);
  await page.locator('.keyframe').first().hover();await page.locator('#keyframe-overlay').waitFor();
  await page.locator('video').evaluate(v=>{v.loop=true;return v.play()});assert.equal(await page.locator('video').evaluate(v=>v.paused),false);
  await page.getByRole('button',{name:'收起',exact:true}).click();assert.equal(await page.locator('video').evaluate(v=>v.paused),true);
  assert.equal(await page.locator('#keyframe-overlay').count(),0);assert.equal(await page.locator('.task-item.expanded').count(),0);
  await page.getByRole('button',{name:'查看与审核',exact:true}).click();
  await page.getByRole('button',{name:'通过审核',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('.review-buttons button')?.textContent==='正在归档');
  await page.getByRole('button',{name:'刷新状态',exact:true}).click();
  assert.equal(await page.locator('.task-item.expanded').count(),1,'copying must keep gallery expanded');
  approvalRelease();await page.getByText('制作目录成品：fixture cleanup failure',{exact:false}).waitFor();
  assert.equal(await page.locator('.task-item.expanded').count(),1,'failed output cleanup must remain expanded');
  // Replace the fixture with an unreviewed task to exercise rejection UI and its request.
  delete item.review;delete item.cleanup;batch.updated_at++;await page.getByRole('button',{name:'刷新状态',exact:true}).click();
  page.once('dialog',d=>d.accept());await page.getByRole('button',{name:'审核不通过',exact:true}).click();
  await page.waitForFunction(()=>!document.querySelector('.task-item'));assert.equal(rejected,true);
  assert.equal(await page.locator('#keyframe-overlay').count(),0);
  assert.deepEqual(errors,[]);
  await page.screenshot({path:path.join(artifact,'ui-after-rejection.png'),fullPage:true});
  console.log(JSON.stringify({ok:true,tests:['explicit gallery load','paired gallery display','collapse pauses playback','collapse closes hover','approval copying preserves expansion','cleanup failure preserves expansion','rejection confirmation and API','removed task dismisses overlay'],pageErrors:errors},null,2));
 } finally {await browser.close()}
})().catch(e=>{console.error(e.stack);process.exit(1)});

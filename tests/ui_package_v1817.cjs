const {chromium}=require('playwright');const assert=require('assert');
(async()=>{const browser=await chromium.launch({headless:true,executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});try{
 const page=await browser.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));page.on('dialog',d=>d.accept());
 await page.goto(process.env.MIXCUT_TEST_URL+'/#tasks');await page.getByText('本地服务已连接',{exact:false}).first().waitFor();
 const pending=page.waitForResponse(r=>r.url().endsWith('/api/tasks/clear')&&r.request().method()==='POST',{timeout:15000});
 await page.locator('#clear-all').click();const response=await pending;assert.equal(response.status(),200);const data=await response.json();assert.equal(data.deleted_items,72);
 await page.waitForFunction(()=>document.querySelectorAll('#batches .task-item').length===0);assert.deepEqual(errors,[]);
 console.log('V1.8.17 real browser Delete All passed: HTTP200,72 removed, no task rows or page errors.');
}finally{await browser.close()}})().catch(e=>{console.error(e);process.exit(1)});

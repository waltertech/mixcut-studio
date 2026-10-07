const {chromium}=require('playwright');const assert=require('assert');
(async()=>{const browser=await chromium.launch({headless:true,executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});const page=await browser.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));try{
 await page.goto(process.env.MIXCUT_TEST_URL+'/#tasks');await page.getByText('本地服务已连接',{exact:false}).first().waitFor();
 const row=page.locator('.task-item').filter({has:page.locator(`[data-task-key="${process.env.MIXCUT_TASK_KEY}"]`)});
 const task=page.locator(`[data-task-key="${process.env.MIXCUT_TASK_KEY}"]`);
 await task.locator('.item-controls').getByRole('button',{name:'删除任务',exact:true}).click();
 await task.waitFor({state:'detached'});
 assert.ok((await page.locator('#toast').textContent()).includes('所有文件保留不变'));assert.deepEqual(errors,[]);
 console.log('V1.5.8 packaged browser: delete task button removes row and reports files retained; no page errors');
}finally{await browser.close();}})().catch(e=>{console.error(e);process.exit(1)});

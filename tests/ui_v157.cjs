const {chromium}=require('playwright');const assert=require('assert');
(async()=>{const browser=await chromium.launch({headless:true,executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});const page=await browser.newPage({viewport:{width:1400,height:1000}});const errors=[];page.on('pageerror',e=>errors.push(e.message));try{
 await page.goto(process.env.MIXCUT_TEST_URL+'/#tasks');await page.getByText('本地服务已连接',{exact:false}).first().waitFor();
 assert.equal(await page.locator('.task-review-setting').isVisible(),false);
 assert.equal(await page.locator('#review-dir').isVisible(),false);
 const buttons=page.getByRole('button',{name:'打开成品文件夹',exact:true});assert.ok(await buttons.count()>0);
 await page.getByRole('button',{name:'查看与审核',exact:true}).first().click();
 await page.getByRole('button',{name:'通过审核',exact:true}).first().click();
 await page.getByText('已通过审核，保留原处：',{exact:false}).first().waitFor();
 assert.equal(await page.locator('#toast').textContent(),'已通过审核，成片保留原处。');
 assert.deepEqual(errors,[]);console.log('V1.5.7 packaged browser approval: hidden archive settings, per-task folder button, retained approval passed');
}finally{await browser.close();}})().catch(e=>{console.error(e);process.exit(1)});

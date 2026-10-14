/* Independent music-led page. Never reads or changes the recorded-video planner. */
(() => {
  'use strict';
  const $ = s => document.querySelector(s);
  const selected = new Set(), cards = new Map();
  let tasks = [], polling = null, fetching = false, busy = false;
  const active = new Set(['running','preparing','thumbnails','queued']);
  const labels = {draft:'待开始',queued:'等待制作',preparing:'准备中',running:'制作中',thumbnails:'生成缩略图',paused:'已暂停',stalled_paused:'执行被卡住暂停',failed:'失败',success:'可审核',approved:'已审核',rejected:'审核不通过'};
  const fmt = n => { n = Math.round(n || 0); return `${Math.floor(n/3600)}:${String(Math.floor(n%3600/60)).padStart(2,'0')}:${String(n%60).padStart(2,'0')}`; };
  const message = (text, error=false) => { $('#loop-message').textContent=text; $('#loop-message').className=error?'notice error':'notice'; };
  const api = async (url, body) => {
    const response = await fetch(url, body === undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const result = await response.json(); if(!response.ok) throw new Error(result.error || '请求失败'); return result;
  };
  const element = (tag, className, text) => { const el=document.createElement(tag); if(className)el.className=className; if(text!==undefined)el.textContent=text; return el; };
  const button = (text, fn) => { const b=element('button','button',text); b.type='button'; b.onclick=async()=>{if(b.disabled)return;b.disabled=true;try{await fn();}catch(e){message(e.message,true);}finally{b.disabled=false;}}; return b; };
  const close = card => { card.detail.hidden=true; card.video?.pause(); if(card.toggle) card.toggle.textContent='查看与审核'; };
  const act = async (id, action) => { await api(`/api/music-loop/${id}/${action}`,action==='start'?{parallel_tasks:Number($('#loop-parallel').value)}:{}); await refresh(); };
  const draw = list => {
    tasks=list;
    for(const [id,card] of cards) if(!list.some(t=>t.id===id)){close(card);card.node.remove();cards.delete(id);selected.delete(id);}
    $('#loop-empty').hidden=!!list.length;
    for(const task of list){
      let card=cards.get(task.id);
      if(!card){
        card={node:element('article','panel loop-task'), detail:element('div','loop-details')}; card.detail.hidden=true;
        card.header=element('div','loop-heading'); card.check=element('input');card.check.type='checkbox';card.check.setAttribute('aria-label','选择音乐循环任务');
        card.check.onchange=()=>{card.check.checked?selected.add(task.id):selected.delete(task.id);};
        card.title=element('strong');card.meta=element('p','field-help');card.stage=element('p');card.error=element('p','error');card.actions=element('div','button-row');
        card.header.append(card.check,card.title);card.node.append(card.header,card.meta,card.stage,card.error,card.actions,card.detail);
        cards.set(task.id,card);$('#loop-list').append(card.node);
      }
      card.title.textContent=`${String(list.indexOf(task)+1).padStart(2,'0')} · ${task.name}`;
      card.meta.textContent=`${task.music.length} 首 · ${fmt(task.actual_duration || task.duration)} · ${labels[task.status] || task.status} · ${Math.round(task.progress*100)}%`;
      card.stage.textContent=task.stage || '';card.error.textContent=task.error || '';card.error.hidden=!task.error;
      if(card.status!==task.status){
        card.status=task.status;card.actions.replaceChildren();
        if(['draft','paused','stalled_paused','failed'].includes(task.status))card.actions.append(button(task.status==='failed'?'重试':'开始制作',()=>act(task.id,'start')));
        if(active.has(task.status))card.actions.append(button('暂停',()=>act(task.id,'pause')));
        if(task.status==='success'){
          card.toggle=button('查看与审核',async()=>{
            if(!card.detail.hidden){close(card);return;}
            card.detail.hidden=false;card.toggle.textContent='收起';
            if(card.loadedOutput===task.output_path)return;
            card.detail.replaceChildren();
            const controls=element('div','button-row');
            controls.append(button('通过审核',()=>act(task.id,'approve')),button('收起',()=>close(card)),button('审核不通过',async()=>{
              if(confirm('仅将这次生成的作品目录移入废纸篓；原音乐、动态封面及上游文件保留。确认审核不通过？'))await act(task.id,'reject');
            }));
            card.video=element('video');card.video.controls=true;card.video.preload='none';card.video.src=`/api/music-loop/output?id=${task.id}`;
            const order=element('details');order.append(element('summary',null,'查看固定曲目顺序'));const ol=element('ol');task.music.forEach(song=>ol.append(element('li',null,song.name)));order.append(ol);
            card.detail.append(controls,card.video,order);card.loadedOutput=task.output_path;
          });card.actions.append(card.toggle);
        }else if(!['success'].includes(task.status)){close(card);}
        if(['draft','paused','failed','stalled_paused'].includes(task.status))card.actions.append(button('修复音乐路径',async()=>{
          const root=$('#loop-music-root').value.trim();if(!root)throw new Error('请先填写上方的音乐新根目录');await api(`/api/music-loop/${task.id}/relocate`,{music_root:root});await refresh();
        }));
        if(['success','approved'].includes(task.status))card.actions.append(button('打开成品文件夹',()=>api('/api/music-loop/reveal',{id:task.id})));
        card.actions.append(button('删除任务',()=>act(task.id,'delete')));
      }
    }
  };
  async function refresh(){if(fetching)return;fetching=true;try{const data=await api('/api/music-loop');draw(data.tasks);}catch(e){message(e.message,true);}finally{fetching=false;}}
  const bulk = async action => {
    if(busy)return;busy=true;
    try{const targets=tasks.filter(t=>selected.has(t.id));if(!targets.length)throw new Error('请先选择任务');if(action==='start'){await api('/api/music-loop/start',{ids:targets.map(t=>t.id),parallel_tasks:Number($('#loop-parallel').value)});}else{for(const t of targets)await api(`/api/music-loop/${t.id}/${action}`,{});}await refresh();}
    catch(e){message(e.message,true);}finally{busy=false;}
  };
  const viewChanged=()=>{
    for(const card of cards.values())if(location.hash!=='#music-loop')card.video?.pause();
    if(polling)clearInterval(polling);polling=null;
    if(location.hash==='#music-loop'){refresh();polling=setInterval(refresh,3000);}
  };
  document.addEventListener('DOMContentLoaded',()=>{
    $('#loop-import').onclick=async()=>{
      const b=$('#loop-import');b.disabled=true;
      try{
        const data=await api('/api/music-loop/import',{paths:$('#loop-paths').value.split('\n').map(x=>x.trim()).filter(Boolean),output_dir:$('#loop-output').value.trim(),music_root:$('#loop-music-root').value.trim()||undefined,config:{resolution:$('#loop-resolution').value,fps:Number($('#loop-fps').value),music_volume:Number($('#loop-volume').value),nonstop_music:$('#loop-nonstop').checked,parallel_tasks:Number($('#loop-parallel').value)}});
        draw(data.tasks);message(`已导入 ${data.imported.length} 条。${data.errors.map(e=>e.path+'：'+e.error).join('\n')}`,!!data.errors.length);
      }catch(e){message(e.message,true);}finally{b.disabled=false;}
    };
    for(const [id,target,kind] of [['loop-pick','loop-paths','music'],['loop-output-pick','loop-output','output'],['loop-music-pick','loop-music-root','music']])$('#'+id).onclick=async()=>{try{const result=await api('/api/pick-folder',{kind,persist:false,current_path:$('#'+target).value});if(result.path)$('#'+target).value=result.path;}catch(e){message(e.message,true);}};
    $('#loop-select').onclick=()=>{const all=tasks.every(t=>selected.has(t.id));for(const t of tasks){all?selected.delete(t.id):selected.add(t.id);cards.get(t.id).check.checked=!all;}};
    $('#loop-start').onclick=()=>bulk('start');$('#loop-pause').onclick=()=>bulk('pause');$('#loop-delete').onclick=()=>bulk('delete');
    $('#loop-approve').onclick=async()=>{if(busy)return;busy=true;try{for(const t of tasks.filter(t=>t.status==='success'))await api(`/api/music-loop/${t.id}/approve`,{});await refresh();message('可审核任务已按顺序通过审核，成品保留原处。');}catch(e){message(e.message,true);}finally{busy=false;}};
    $('#loop-refresh').onclick=refresh;
    $('#loop-cache').onclick=async()=>{try{const data=await api('/api/music-loop/cache');$('#loop-cache-info').textContent=`本模式缓存 ${(data.bytes/1024**3).toFixed(2)} GB · ${data.files} 个文件`;}catch(e){message(e.message,true);}};
    $('#loop-clear-cache').onclick=async()=>{try{const data=await api('/api/music-loop/cache/clear',{});message(`已清理 ${data.removed} 条任务的缓存，保留 ${data.skipped} 条正在使用的任务。`);await refresh();$('#loop-cache').click();}catch(e){message(e.message,true);}};
    document.addEventListener('mixcut-bootstrap',()=>{if(!$('#loop-output').value)$('#loop-output').value=$('#output-dir').value;});
    window.addEventListener('hashchange',viewChanged);viewChanged();
  });
})();

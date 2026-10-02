(() => {
  'use strict';
  const state = { scan: { videos: [], music: [], errors: [] }, batch: null, batches: [], poller: null, scanPoller: null, scanSignature: '', scanRunning: false, libraryDirs: null, dirtyLibrary: new Set(), mediaCache: new Map(), keyframeObserver: null, batchSignature: '', batchLayoutSignature: '', serverVersion: '', reviewJobs: new Map() };
  const selectedTasks = new Set();
  const taskKey = (batchId, itemId) => `${batchId}:${itemId}`;
  const taskSelection = (batch, item) => { const checkbox = document.createElement('input'); checkbox.type = 'checkbox'; checkbox.className = 'task-select'; checkbox.setAttribute('aria-label', `选择任务 ${item.index || item.id}`); const key = taskKey(batch.id,item.id); checkbox.checked = selectedTasks.has(key); checkbox.addEventListener('change', () => { if (checkbox.checked) selectedTasks.add(key); else selectedTasks.delete(key); updateSelectionButtons(); }); return checkbox; };
  const $ = (s, root = document) => root.querySelector(s);
  const $$ = (s, root = document) => [...root.querySelectorAll(s)];
  const updateSelectionButtons = () => {
    const planCount = (state.batch?.items || []).filter(item => selectedTasks.has(taskKey(state.batch.id, item.id))).length;
    const taskCount = state.batches.reduce((total, batch) => total + (batch.items || []).filter(item => selectedTasks.has(taskKey(batch.id, item.id))).length, 0);
    $('#delete-selected-plan').textContent = planCount ? `删除选中任务（${planCount}）` : '删除选中任务';
    $('#delete-selected-tasks').textContent = taskCount ? `删除选中任务（${taskCount}）` : '删除选中任务';
  };
  const fmt = (seconds) => { const raw = Number(seconds); if (!Number.isFinite(raw)) return '未知时长'; const total = Math.max(0, Math.round(raw)), h = Math.floor(total / 3600), m = Math.floor(total % 3600 / 60), s = total % 60; return (h ? `${h}:` : '') + `${String(m).padStart(h ? 2 : 1, '0')}:${String(s).padStart(2, '0')}`; };
  const safeClass = (value) => String(value || 'pending').toLowerCase().replace(/[^a-z0-9_-]/g, '');
  const toast = (message, isError = false) => { const el = $('#toast'); el.textContent = message; el.style.background = isError ? '#9c4035' : ''; el.classList.add('show'); clearTimeout(toast.timer); toast.timer = setTimeout(() => el.classList.remove('show'), 4200); };
  const api = async (path, options = {}) => { let res; try { res = await fetch(path, { headers: { 'Content-Type': 'application/json', ...(options.headers || {}) }, ...options }); } catch (_) { throw new Error('无法连接本地服务。请确认启动器仍在运行，然后刷新页面。'); } let data = null; try { data = await res.json(); } catch (_) {} if (!res.ok) { if (res.status === 404 && data?.error === '接口不存在') throw new Error('本地后台仍是旧版进程。请关闭后台后重新打开应用。'); throw new Error((data && (data.error || data.message || data.detail)) || `请求失败（${res.status}）`); } return data; };
  const setServer = (online, message) => { const el = $('#server-state'); el.className = `server-state ${online ? 'online' : 'error'}`; el.lastChild.textContent = ` ${message}`; };
  const connectedLabel = () => `本地服务已连接${state.serverVersion ? ` · V${state.serverVersion}` : ''}`;
  const currentView = () => location.hash.slice(1) || 'assets';
  const showView = () => { const view = currentView(); $$('.view').forEach(el => el.hidden = el.id !== view); $$('.nav a').forEach(el => el.classList.toggle('active', el.dataset.view === view)); if (view === 'tasks') refreshBatches(); else stopPolling(); };
  const stopPolling = () => { if (state.poller) clearInterval(state.poller); state.poller = null; };
  const setupPolling = () => { stopPolling(); state.poller = setInterval(() => { if (currentView() === 'tasks') refreshBatches(true); }, 3000); };
  const renderErrors = (errors = []) => { const box = $('#scan-errors'); box.replaceChildren(); if (!errors.length) { box.hidden = true; return; } errors.forEach(error => { const line = document.createElement('div'); line.textContent = typeof error === 'string' ? error : (error.message || JSON.stringify(error)); box.append(line); }); box.hidden = false; };
  const assetMeta = (asset) => [fmt(asset.duration), asset.width && asset.height ? `${asset.width}×${asset.height}` : '', asset.fps ? `${asset.fps} fps` : ''].filter(Boolean).join(' · ');
  const thumbnail = (asset) => `/api/thumbnail?id=${encodeURIComponent(asset.id)}`;
  const assetPrefs = () => { try { return JSON.parse(localStorage.getItem('mixcut.asset-prefs') || '{}'); } catch (_) { return {}; } };
  const saveAssetPrefs = () => { const prefs = assetPrefs(); $$('.video-card').forEach(card => { const id = $('.asset-select', card).dataset.id; prefs[id] = { selected: $('.asset-select', card).checked, group: $('.group-input', card).value }; }); $$('.music-card').forEach(card => { const id = $('.asset-select', card).dataset.id; prefs[id] = { selected: $('.asset-select', card).checked }; }); try { localStorage.setItem('mixcut.asset-prefs', JSON.stringify(prefs)); } catch (_) {} };
  const media = (asset) => `/api/media?id=${encodeURIComponent(asset.id)}`;
  const clearScannedLibrary = (kind) => { state.dirtyLibrary.add(kind); state.scan[kind === 'video' ? 'videos' : 'music'] = []; renderAssets(); $('#scan-progress').textContent = `${kind === 'video' ? '视频' : '音乐'}路径已更改，请刷新该素材库。`; $('#plan-btn').disabled = true; };
  const persistPreference = async (key, value) => { if (!value) return; try { await api('/api/preferences', { method: 'POST', body: JSON.stringify({ [key]: value }) }); } catch (error) { toast(`保存目录失败：${error.message}`, true); } };
  const syncReviewDir = (value) => { $('#review-dir').value = value; $('#task-review-dir').value = value; };
  const approveItem = async (batchId, itemId, button) => { const review_dir = $('#task-review-dir').value.trim() || $('#review-dir').value.trim(); if (!review_dir) { toast('请先选择审核通过文件夹。', true); return; } button.disabled = true; button.textContent = '正在归档'; try { await api('/api/approve', { method: 'POST', body: JSON.stringify({ batch_id: batchId, item_id: itemId, review_dir }) }); toast('已复制到审核通过文件夹。'); await refreshBatches(); } catch (error) { button.disabled = false; button.textContent = '通过审核'; const line = document.createElement('div'); line.className = 'error'; line.textContent = `归档失败：${error.message}`; button.parentElement?.append(line); toast(error.message, true); } };
  const reviewCandidates = batch => (batch.items || []).filter(item => item.status === 'success' && (!item.thumbnails || item.thumbnails.status === 'ready') && !['approved','superseded','copying'].includes(item.review?.status)).sort((a,b) => (Number(a.index) || 0) - (Number(b.index) || 0) || String(a.id).localeCompare(String(b.id)));
  const reviewPolls = new Set();
  const paintReviewJob = batchId => {
    const card = $(`.batch[data-batch-id="${batchId}"]`), batch = state.batches.find(entry => entry.id === batchId);
    if (!card || !batch) return;
    const job = state.reviewJobs.get(batchId), button = $('.batch-approve-all', card);
    if (button) button.disabled = job?.status === 'running' || !reviewCandidates(batch).length;
    const note = $('.batch-review-state', card);
    if (!note) return;
    note.textContent = job?.status === 'running' ? `审核中 ${job.completed}/${job.total}，当前任务 ${job.current_item || '准备中'}` : job?.status === 'failed' ? `审核停在任务 ${job.failed_item}：${job.error}。修复后可再次点击。` : '';
    note.className = `batch-review-state ${job?.status === 'failed' ? 'error' : 'field-help'}`;
  };
  const watchBatchReview = async batchId => {
    if (reviewPolls.has(batchId)) return;
    reviewPolls.add(batchId);
    try {
      while (true) {
        const job = await api(`/api/batches/${encodeURIComponent(batchId)}/approve-all`);
        state.reviewJobs.set(batchId, job);
        state.batchSignature = '';
        await refreshBatches();
        paintReviewJob(batchId);
        if (job.status !== 'running') {
          toast(job.status === 'completed' ? `批次审核完成：${job.completed}/${job.total} 条` : `批次审核停在任务 ${job.failed_item || '未知'}：${job.error || '未知错误'}`, job.status !== 'completed');
          break;
        }
        await new Promise(resolve => setTimeout(resolve, 2500));
      }
    } catch (error) { toast(`无法获取批次审核状态：${error.message}`, true); }
    finally { reviewPolls.delete(batchId); }
  };
  const approveBatch = async batch => {
    const review_dir = $('#task-review-dir').value.trim() || $('#review-dir').value.trim();
    if (!review_dir) return toast('请先选择审核通过文件夹。', true);
    const card = $(`.batch[data-batch-id="${batch.id}"]`);
    if ($$('.sticker-variant', card).some(select => select.value)) return toast('本批次已选择贴图模板，请先处理贴图任务。', true);
    const candidates = reviewCandidates(batch);
    if (!candidates.length) return toast('此批次没有可审核的已完成任务。', true);
    const skipped = (batch.items || []).length - candidates.length;
    if (!confirm(`将按任务序号依次审核 ${candidates.length} 条已完成视频${skipped ? `，跳过 ${skipped} 条未完成或已审核任务` : ''}。每条归档成功后会按现有规则清理制作目录成片及可安全删除的原素材；遇到错误即停止。确定继续吗？`)) return;
    try {
      const job = await api(`/api/batches/${encodeURIComponent(batch.id)}/approve-all`, {method:'POST', body:JSON.stringify({review_dir, item_ids:candidates.map(item => String(item.id))})});
      state.reviewJobs.set(batch.id, job);
      state.batchSignature = '';
      await refreshBatches();
      watchBatchReview(batch.id);
    } catch (error) { toast(error.message, true); }
  };
  let globalReviewPolling = false;
  const watchAllReview = async () => {
    if (globalReviewPolling) return;
    globalReviewPolling = true;
    const button = $('#approve-all-visible');
    try {
      while (true) {
        const job = await api('/api/tasks/approve-all');
        if (job.status === 'idle') break;
        button.disabled = job.status === 'running';
        button.textContent = job.status === 'running' ? `审核中 ${job.completed}/${job.total}` : '一键审核';
        await refreshBatches();
        if (job.status !== 'running') {
          toast(job.status === 'completed' ? `已审核 ${job.completed} 条任务。` : `审核停在任务 ${job.failed_item || '未知'}：${job.error || '未知错误'}`, job.status !== 'completed');
          break;
        }
        await new Promise(resolve => setTimeout(resolve, 2500));
      }
    } catch (error) { toast(`无法获取审核状态：${error.message}`, true); }
    finally { button.disabled = false; button.textContent = '一键审核'; globalReviewPolling = false; }
  };
  const approveAllVisible = async () => {
    const review_dir = $('#task-review-dir').value.trim() || $('#review-dir').value.trim();
    if (!review_dir) return toast('请先选择审核通过文件夹。', true);
    if ($$('.sticker-variant').some(select => select.value)) return toast('有任务选了贴图模板，请先生成对应的贴图版本。', true);
    const count = state.batches.reduce((total,batch) => total + reviewCandidates(batch).length, 0);
    if (!count) return toast('没有待审核的已完成任务。');
    if (!confirm(`将依次审核 ${count} 条已完成任务；审核成功的任务会从列表移除。确定继续吗？`)) return;
    try { await api('/api/tasks/approve-all', {method:'POST', body:JSON.stringify({review_dir})}); watchAllReview(); }
    catch (error) { toast(error.message, true); }
  };
  const pickFolder = async (kind) => { const input = kind === 'review' ? $('#review-dir') : $(`#${kind}-dir`); if (!input) return; const buttons = $$('.folder-picker'); buttons.forEach(button => button.disabled = true); try { const data = await api('/api/pick-folder', { method: 'POST', body: JSON.stringify({ kind, current_path: input.value.trim() }) }); if (data.cancelled || !data.path) return; const changed = input.value.trim() !== data.path; input.value = data.path; if (kind === 'review') syncReviewDir(data.path); if ((kind === 'video' || kind === 'music') && changed) clearScannedLibrary(kind); if (kind === 'output' || kind === 'review') toast('已保存目录。'); } catch (error) { toast(error.message, true); } finally { buttons.forEach(button => button.disabled = false); } };
  const frameTime = (value) => { const seconds = Math.max(0, Number(value) || 0), minutes = Math.floor(seconds / 60), remainder = seconds % 60; return `${String(minutes).padStart(2, '0')}:${remainder.toFixed(3).padStart(6, '0')}`; };
  const frameUrl = (batch, item, index, size, version) => `/api/keyframe?batch=${encodeURIComponent(batch)}&item=${encodeURIComponent(item)}&index=${encodeURIComponent(index)}&size=${size}&v=${encodeURIComponent(version || '')}`;
  const batchesSignature = (batches) => JSON.stringify((batches || []).map(batch => [batch.id, batch.updated_at, batch.status, batch.output_folder, ...(batch.items || []).map(item => [item.id, item.status, item.progress, item.result?.output_size, item.result?.output_mtime_ns, item.review?.status, item.review?.path, item.review?.error, item.review?.approved_at, item.error, item.thumbnails, item.render_stage, item.music_detection]) ]));
  const batchesLayoutSignature = (batches) => JSON.stringify((batches || []).map(batch => [batch.id, batch.status, batch.error, batch.recovery_note, batch.folder_name, batch.output_folder, (batch.items || []).map(item => [item.id, item.index, item.kind, item.status, item.error, item.output_name, item.duration, item.result, item.review, item.cleanup, item.thumbnails])]));
  const taskScrollAnchor = () => { const rows = $$('.task-item[data-task-key]'); const row = rows.find(node => { const rect = node.getBoundingClientRect(); return rect.bottom > 100 && rect.top < innerHeight; }); return { key: row?.dataset.taskKey, top: row?.getBoundingClientRect().top, y: window.scrollY }; };
  const restoreTaskScroll = anchor => { if (!anchor || currentView() !== 'tasks') return; const row = $$('.task-item[data-task-key]').find(node => node.dataset.taskKey === anchor.key); if (row) window.scrollBy(0, row.getBoundingClientRect().top - anchor.top); else window.scrollTo(0, anchor.y); };
  const updateTaskProgress = batches => { const rows = new Map($$('.task-item[data-task-key]').map(row => [row.dataset.taskKey, row])); batches.forEach(batch => (batch.items || []).forEach(item => { const label = rows.get(taskKey(batch.id, item.id))?.querySelector('.task-label'); if (!label) return; const pct = Number.isFinite(Number(item.progress)) ? ` · ${Math.round(Number(item.progress) * 100)}%` : ''; const next = `${item.output_name ? item.output_name + ' · ' : ''}${fmt(item.duration)}${pct}${item.status === 'running' && item.render_stage === 'silence_detection' ? ` · 正在进行音乐静音检测（${item.music_detection?.done || 0}/${item.music_detection?.total || 0} 首）` : ''}`; if (label.firstChild?.nodeType === Node.TEXT_NODE) label.firstChild.nodeValue = next; else label.textContent = next; })); };
  const hideFrameOverlay = () => { const overlay = $('#keyframe-overlay'); if (overlay) overlay.remove(); };
  const showFrameOverlay = (event, frame) => { hideFrameOverlay(); const overlay = document.createElement('div'); overlay.id = 'keyframe-overlay'; overlay.className = 'keyframe-overlay'; const image = document.createElement('img'); image.alt = `关键帧大图 ${frame.label}`; image.src = frame.large; const caption = document.createElement('span'); caption.textContent = `关键帧 · ${frame.label}`; overlay.append(image, caption); document.body.append(overlay); const x = Math.min((event.clientX || frame.button.getBoundingClientRect().right) + 14, innerWidth - overlay.offsetWidth - 10), y = Math.min((event.clientY || frame.button.getBoundingClientRect().bottom) + 14, innerHeight - overlay.offsetHeight - 10); overlay.style.left = `${Math.max(10, x)}px`; overlay.style.top = `${Math.max(10, y)}px`; };
  const addFrameButtons = (gallery, frames, start) => { const grid = $('.keyframe-grid', gallery), batch = gallery.dataset.batch, item = gallery.dataset.item, version = gallery.dataset.version; frames.slice(start).forEach(frame => { const button = document.createElement('button'); button.type = 'button'; button.className = 'keyframe'; const label = frameTime(frame.time); button.setAttribute('aria-label', `跳转到关键帧 ${label}`); const image = document.createElement('img'); image.loading = 'lazy'; image.alt = `关键帧 ${label}`; image.src = frameUrl(batch, item, frame.index, 'thumb', version); const time = document.createElement('time'); time.textContent = label; button.append(image, time); const descriptor = { button, label, large: frameUrl(batch, item, frame.index, 'large', version) }; button.addEventListener('pointerenter', event => showFrameOverlay(event, descriptor)); button.addEventListener('focus', event => showFrameOverlay(event, descriptor)); button.addEventListener('pointerleave', hideFrameOverlay); button.addEventListener('blur', hideFrameOverlay); button.addEventListener('click', () => { const video = gallery.closest('.task-item')?.querySelector('video'); if (video) seekFrame(video, frame.time); }); grid.append(button); }); };
  const seekFrame = (video, value) => { if (!video) return; const target = Number(value) || 0; if (video.readyState >= 1) { const paused = video.paused; video.currentTime = target; if (paused) video.pause(); return; } video.dataset.pendingSeek = String(target); video.preload = 'auto'; if (!video.dataset.waitingMetadata) { video.dataset.waitingMetadata = 'true'; video.addEventListener('loadedmetadata', () => { const latest = Number(video.dataset.pendingSeek); delete video.dataset.waitingMetadata; delete video.dataset.pendingSeek; if (Number.isFinite(latest)) { const paused = video.paused; video.currentTime = latest; if (paused) video.pause(); } }, { once: true }); } video.load(); };
  const renderGalleryFrames = (gallery, frames) => { hideFrameOverlay(); const grid = $('.keyframe-grid', gallery); grid.replaceChildren(); $('h5', gallery).textContent = `每30秒一张 · 共 ${frames.length} 张`; addFrameButtons(gallery, frames, 0); $('.keyframe-more', gallery)?.remove(); };
  const loadGallery = async (gallery) => { if (gallery.dataset.loaded) return; gallery.dataset.loaded = 'loading'; try { const data = await api(`/api/keyframes?batch=${encodeURIComponent(gallery.dataset.batch)}&item=${encodeURIComponent(gallery.dataset.item)}`); const frames = data.frames || []; gallery.dataset.loaded = 'true'; gallery.dataset.version = data.version || ''; renderGalleryFrames(gallery, frames, false); } catch (error) { gallery.dataset.loaded = ''; const heading = $('h5', gallery); heading.textContent = `关键帧暂不可用：${error.message}`; const retry = document.createElement('button'); retry.type = 'button'; retry.className = 'button keyframe-more'; retry.textContent = '重试加载关键帧'; retry.addEventListener('click', () => { retry.remove(); loadGallery(gallery); }); gallery.append(retry); } };
  const observeGallery = (gallery) => { if (!state.keyframeObserver) state.keyframeObserver = new IntersectionObserver(entries => entries.forEach(entry => { if (entry.isIntersecting) { state.keyframeObserver.unobserve(entry.target); loadGallery(entry.target); } }), { rootMargin: '180px 0px' }); state.keyframeObserver.observe(gallery); };
  document.addEventListener('keydown', event => { if (event.key === 'Escape') hideFrameOverlay(); });
  const captureCompletedMedia = () => $$('video[data-media-key]').forEach(video => { const gallery = video.parentElement?.querySelector('.keyframe-gallery'); if (gallery) state.mediaCache.set(video.dataset.mediaKey, { video, gallery, fingerprint: video.dataset.fingerprint || '' }); });
  const hydrateGalleries = () => { $$('video[src*="/api/output?"]').forEach(video => { const url = new URL(video.src, location.href), batch = url.searchParams.get('batch'), item = url.searchParams.get('item'); if (!batch || !item) return; const record = (state.batches.find(entry => String(entry.id) === batch)?.items || []).find(entry => String(entry.id) === item) || {}; if (record.thumbnails && record.thumbnails.status !== 'ready') return; const key = `${batch}:${item}`, fingerprint = `${record.result?.output_size || ''}:${record.result?.output_mtime_ns || ''}`; const cached = state.mediaCache.get(key); if (cached && cached.fingerprint === fingerprint) { video.replaceWith(cached.video); cached.gallery.remove(); cached.video.parentElement?.append(cached.gallery); return; } video.dataset.mediaKey = key; video.dataset.fingerprint = fingerprint; if (video.parentElement?.querySelector('.keyframe-gallery')) return; const gallery = document.createElement('section'); gallery.className = 'keyframe-gallery'; gallery.dataset.batch = batch; gallery.dataset.item = item; gallery.dataset.fingerprint = fingerprint; const heading = document.createElement('h5'); heading.textContent = '缩略图已就绪 · 正在显示'; const grid = document.createElement('div'); grid.className = 'keyframe-grid'; gallery.append(heading, grid); video.parentElement?.append(gallery); observeGallery(gallery); }); };
  function renderAssets() {
    const videos = state.scan.videos || [], music = state.scan.music || [];
    $('#video-count').textContent = `${videos.length} 条`; $('#music-count').textContent = `${music.length} 首`;
    $('#asset-summary').textContent = videos.length || music.length ? `已识别 ${videos.length} 个视频、${music.length} 首音乐。请在下一步选择参与规划的素材。` : '尚未找到可用素材。';
    const videoBox = $('#video-assets'), musicBox = $('#music-assets'); videoBox.replaceChildren(); musicBox.replaceChildren();
    if (!videos.length) videoBox.textContent = '没有可用视频。请检查目录、格式或扫描错误。';
    if (!music.length) musicBox.textContent = '没有可用音乐。请检查目录、格式或扫描错误。';
    const prefs = assetPrefs(); videos.forEach(asset => { const card = $('#video-card-template').content.firstElementChild.cloneNode(true); const image = $('img', card); image.src = thumbnail(asset); image.alt = `${asset.name || '视频'} 缩略图`; image.onerror = () => { image.removeAttribute('src'); image.alt = '缩略图不可用'; }; const check = $('.asset-select', card); check.checked = prefs[asset.id]?.selected ?? true; check.dataset.id = asset.id; $('.asset-name', card).textContent = asset.name || asset.path || '未命名视频'; $('.asset-meta', card).textContent = assetMeta(asset); const group = $('.group-input', card); group.value = prefs[asset.id]?.group ?? asset.group ?? ''; group.dataset.id = asset.id; videoBox.append(card); });
    music.forEach(asset => { const card = $('#music-card-template').content.firstElementChild.cloneNode(true); const check = $('.asset-select', card); check.checked = prefs[asset.id]?.selected ?? true; check.dataset.id = asset.id; $('.asset-name', card).textContent = asset.name || asset.path || '未命名音乐'; $('.asset-meta', card).textContent = [asset.music_style, fmt(asset.duration), asset.duration_precise === false ? '时长待精确校验' : ''].filter(Boolean).join(' · '); $('.audio-preview', card).addEventListener('click', event => { const audio = document.createElement('audio'); audio.controls = true; audio.preload = 'none'; audio.src = media(asset); event.currentTarget.replaceWith(audio); audio.play().catch(() => {}); }); musicBox.append(card); });
    renderPickers();
  }
  function renderPickers() {
    const selectedVideo = $$('.video-card .asset-select:checked').length;
    const selectedMusic = $$('.music-card .asset-select:checked').length;
    $('#selection-summary').textContent = `已选 ${selectedVideo}/${state.scan.videos?.length || 0} 条视频、${selectedMusic}/${state.scan.music?.length || 0} 首音乐。需要排除素材或调整视频分组时，到“素材”页展开列表。`;
  }
  function getConfig() {
    if (state.scanRunning || state.dirtyLibrary.size) throw new Error('请等待素材扫描完成，并刷新已更改路径的素材库。');
    const form = $('#config-form'), values = Object.fromEntries(new FormData(form)); const num = ['parallel_tasks','count','min_source_duration_minutes','min_duration','max_duration','min_songs','max_songs','start_gap','segment_min','segment_max','fps','video_bitrate_mbps','original_volume','music_volume']; num.forEach(key => values[key] = Number(values[key])); [values.width, values.height] = String(values.resolution || '1280x720').split('x').map(Number); delete values.resolution; if (String(values.seed || '').trim()) values.seed = Number(values.seed); else delete values.seed; values.allow_overlap = form.elements.allow_overlap.checked; values.within_group = values.within_group === 'true'; values.output_dir = $('#output-dir').value.trim(); values.video_ids = $$('.video-card .asset-select:checked').map(el => el.dataset.id); values.music_ids = $$('.music-card .asset-select:checked').map(el => el.dataset.id); values.first_song_ids = []; values.groups = Object.fromEntries($$('.group-input').map(el => [el.dataset.id, el.value.trim()]).filter(([, value]) => value));
    values.sticker_template_id = values.sticker_template_id || null; if (!values.video_ids.length) throw new Error('请至少选择一个视频素材。'); if (!values.music_ids.length) throw new Error('请至少选择一首音乐。'); if (!values.output_dir) throw new Error('请在素材页填写导出文件夹。'); if (values.min_duration > values.max_duration) throw new Error('最短时长不能大于最长时长。'); if (values.segment_min > values.segment_max) throw new Error('单段最短时长不能大于最长时长。'); return values;
  }
  function stat(label, value) { const box = document.createElement('div'); box.className = 'stat'; const strong = document.createElement('b'); strong.textContent = value; const span = document.createElement('span'); span.textContent = label; box.append(strong, span); return box; }
  const segmentOverlap = (left, right) => left.asset_id === right.asset_id && Math.min(Number(left.start) + Number(left.duration), Number(right.start) + Number(right.duration)) - Math.max(Number(left.start), Number(right.start)) > 1e-6;
  const planCounts = batch => {
    const songs = new Map(), sources = new Map();
    (batch.items || []).forEach(item => { (item.music || []).forEach(song => songs.set(song.id, (songs.get(song.id) || 0) + 1)); (item.segments || []).forEach(segment => sources.set(segment.asset_id, (sources.get(segment.asset_id) || 0) + 1)); });
    return {songs, sources};
  };
  async function replaceMedia(batch, item, kind, index, button, replanDuration = false) {
    button.disabled = true;
    try {
      const updated = await api(`/api/batches/${encodeURIComponent(batch.id)}/items/${encodeURIComponent(item.id)}/replace`, {method:'POST', body:JSON.stringify({kind,index,replan_duration:replanDuration})});
      state.batches = state.batches.map(entry => entry.id === updated.id ? updated : entry);
      renderPlan(updated);
      toast(kind === 'video' ? '视频替换完成；时长变化时已联动重排音乐。' : '替换完成，整批使用次数已更新。');
    } catch (error) { button.disabled = false; toast(error.message, true); }
  }
  function appendVideoList(root, batch, item, counts) {
    const track = document.createElement('div'); track.className = 'track';
    const title = document.createElement('h4'); title.textContent = '视频区间';
    const list = document.createElement('ol');
    (item.segments || []).forEach((segment, index) => {
      const row = document.createElement('li'); row.className = 'plan-media-row';
      const label = document.createElement('span'); label.textContent = `${segment.path || segment.asset_id} · ${fmt(segment.start)} 起，${fmt(segment.duration)}`;
      const overlapCount = 1 + (batch.items || []).filter(other => other !== item && (other.segments || []).some(candidate => segmentOverlap(segment, candidate))).length;
      const badge = document.createElement('span'); badge.className = 'use-count'; badge.textContent = `片段出现 ${overlapCount} 次 · 源文件使用 ${counts.sources.get(segment.asset_id) || 1} 次`;
      row.append(label, badge);
      if (batch.status === 'draft') { const button = document.createElement('button'); button.type = 'button'; button.className = 'button replace-media'; button.textContent = '随机替换这段视频'; button.title = '优先保持成片时长，必要时联动重排音乐'; button.onclick = () => replaceMedia(batch,item,'video',index,button); row.append(button); const linked = document.createElement('button'); linked.type = 'button'; linked.className = 'button replace-media'; linked.textContent = '联动换视频和音乐'; linked.title = '允许重新选成片时长，并为这条任务重排整组音乐'; linked.onclick = () => replaceMedia(batch,item,'video',index,linked,true); row.append(linked); }
      list.append(row);
    });
    track.append(title,list); root.append(track);
  }
  async function saveMusicOrder(batch, item, musicIds) { try { const updated = await api(`/api/batches/${encodeURIComponent(batch.id)}/items/${encodeURIComponent(item.id)}/music-order`, { method:'POST', body:JSON.stringify({music_ids:musicIds}) }); state.batches = state.batches.map(entry => entry.id === updated.id ? updated : entry); renderPlan(updated); toast('歌曲顺序已保存，将按此顺序渲染。'); } catch (error) { renderPlan(batch); toast(error.message, true); } }
  function appendMusicOrder(root, batch, item, counts) {
    const track = document.createElement('div'); track.className = 'track music-track';
    const h = document.createElement('h4'); h.textContent = '歌曲顺序';
    const editable = batch.status === 'draft' && (item.music?.length || 0) > 1;
    const hint = document.createElement('p'); hint.className = 'music-order-help'; hint.textContent = editable ? '拖动歌曲，或使用箭头调整；修改会立即保存。' : '歌曲顺序已锁定。';
    const list = document.createElement('ol'); list.className = 'music-order';
    const move = (from,to) => { if (from === to || to < 0 || to >= item.music.length) return; const ids = item.music.map(song => song.id); const [value] = ids.splice(from,1); ids.splice(to,0,value); saveMusicOrder(batch,item,ids); };
    (item.music || []).forEach((song,index) => {
      const li = document.createElement('li'); li.dataset.index = String(index); li.draggable = editable;
      const handle = document.createElement('span'); handle.className = 'drag-handle'; handle.textContent = editable ? '⠿' : '•'; handle.title = editable ? '拖动调整顺序' : '';
      const name = document.createElement('span'); name.className = 'music-name'; name.textContent = song.name || song.path || song.asset_id || String(song);
      const badge = document.createElement('span'); badge.className = 'use-count'; badge.textContent = `本批使用 ${counts.songs.get(song.id) || 1} 次`;
      li.append(handle,name,badge);
      if (batch.status === 'draft') {
        const replace = document.createElement('button'); replace.type = 'button'; replace.className = 'button replace-media'; replace.textContent = '随机替换这首歌'; replace.onclick = () => replaceMedia(batch,item,'music',index,replace); li.append(replace);
      }
      if (editable) {
        const controls = document.createElement('span'); controls.className = 'order-buttons';
        [['↑',index-1,'上移'],['↓',index+1,'下移']].forEach(([label,target,title]) => { const button = document.createElement('button'); button.type = 'button'; button.textContent = label; button.title = title; button.disabled = target < 0 || target >= item.music.length; button.onclick = () => move(index,target); controls.append(button); });
        li.append(controls);
        li.addEventListener('dragstart',event => { event.dataTransfer.effectAllowed = 'move'; event.dataTransfer.setData('text/plain',String(index)); li.classList.add('dragging'); });
        li.addEventListener('dragend',() => li.classList.remove('dragging'));
        li.addEventListener('dragover',event => { event.preventDefault(); event.dataTransfer.dropEffect = 'move'; li.classList.add('drag-over'); });
        li.addEventListener('dragleave',() => li.classList.remove('drag-over'));
        li.addEventListener('drop',event => { event.preventDefault(); li.classList.remove('drag-over'); move(Number(event.dataTransfer.getData('text/plain')),index); });
      }
      list.append(li);
    });
    track.append(h,hint,list);
    if (batch.status === 'draft') {
      const refresh = document.createElement('button'); refresh.type = 'button'; refresh.className = 'button'; refresh.textContent = '刷新这条音乐';
      refresh.onclick = async () => { refresh.disabled = true; try { const updated = await api(`/api/batches/${encodeURIComponent(batch.id)}/items/${encodeURIComponent(item.id)}/refresh-music`,{method:'POST',body:'{}'}); state.batches = state.batches.map(entry => entry.id === updated.id ? updated : entry); renderPlan(updated); toast('已重新分配音乐，整批使用次数已更新。'); } catch (error) { refresh.disabled = false; toast(error.message,true); } };
      track.append(refresh);
    }
    root.append(track);
  }
  function renderPlan(batch) {
    state.batch = batch; $('#plan-message').textContent = batch ? `当前方案共 ${batch.items?.length || 0} 条任务。核对后开始渲染。${batch.output_folder ? ` 输出目录：${batch.output_folder}` : ''}` : '尚未生成方案。'; $('#start-btn').disabled = !batch || !batch.items?.length || batch.status !== 'draft'; $('#manifest-btn').disabled = !batch; const stats = $('#plan-stats'), warnings = $('#plan-warnings'), list = $('#plan-items'); stats.replaceChildren(); warnings.replaceChildren(); list.replaceChildren(); if (!batch) { updateSelectionButtons(); return; }
    if(batch.config?.sticker_layers?.length){const note=document.createElement('div');note.textContent=`贴图模板：${batch.config.sticker_template?.name || '已保存模板'} · ${batch.config.sticker_layers.length}层（位置、大小和出现时间已固定到本批次）`;warnings.append(note);}
    const s = batch.stats || {};
    const numberOrDash = value => Number.isFinite(Number(value)) ? Number(value) : '—';
    stats.append(stat('当前方案', batch.items?.length ?? 0), stat('原定数量', batch.config?.count ?? '—'), stat('并行剪辑', batch.config?.parallel_tasks || 1));
    if (s.unique_music_orders !== undefined) stats.append(stat('不同歌曲顺序', numberOrDash(s.unique_music_orders)));
    if (s.unique_video_plans !== undefined) stats.append(stat('不同视频方案', numberOrDash(s.unique_video_plans)));
    if (s.segment_overlap_pairs !== undefined) stats.append(stat('片段重合对数', numberOrDash(s.segment_overlap_pairs)));
    if (Number.isFinite(Number(s.max_overlap_ratio))) stats.append(stat('最高两两重合', `${Math.round(Number(s.max_overlap_ratio) * 100)}%`));
    if (Number.isFinite(Number(s.estimated_output_bytes))) stats.append(stat('预计输出大小', `${(Number(s.estimated_output_bytes) / 1024 ** 3).toFixed(2)} GB`));
    if (s.total_duration !== undefined) stats.append(stat('总成片时长', fmt(s.total_duration)));
    (batch.warnings || []).forEach(w => { const line = document.createElement('div'); line.textContent = typeof w === 'string' ? w : (w.message || JSON.stringify(w)); warnings.append(line); });
    const counts = planCounts(batch); (batch.items || []).forEach(item => { const card = document.createElement('article'); card.className = 'plan-item'; const head = document.createElement('div'); head.className = 'item-head'; const title = document.createElement('h3'); title.textContent = item.output_name || `成片 ${item.index ?? '—'}`; const duration = document.createElement('span'); duration.className = 'duration'; duration.textContent = fmt(item.duration); head.append(taskSelection(batch,item), title, duration); const tracks = document.createElement('div'); tracks.className = 'tracks'; appendVideoList(tracks,batch,item,counts); appendMusicOrder(tracks,batch,item,counts); card.append(head, tracks); list.append(card); });
    updateSelectionButtons();
  }
  const stopScanPolling = () => { if (state.scanPoller) clearTimeout(state.scanPoller); state.scanPoller = null; };
  async function pollScan() { try { applyScanJob(await api('/api/scan/status?compact=1')); } catch (error) { stopScanPolling(); state.scanRunning = false; $('#scan-btn').disabled = false; $('#scan-btn').textContent = '扫描素材'; toast(error.message, true); } }
  function applyScanJob(job) {
    if (!job || job.status === 'idle') return;
    const running = ['discovering', 'analyzing'].includes(job.status);
    state.scanRunning = running;
    $('#scan-btn').disabled = running;
    $('#scan-btn').textContent = running ? '正在扫描…' : '扫描素材';
    $('#plan-btn').disabled = running || !!state.dirtyLibrary.size;
    const totals = job.totals || {}, processed = job.processed || {};
    const scanSummary = job.scan?.summary || {};
    const summaryLine = kind => { const value = scanSummary[kind]; return value ? `${kind === 'video' ? '视频' : '音乐'}目录 ${value.files} 个文件，按格式跳过 ${value.skipped_extension} 个，按内容跳过 ${value.skipped_content} 个，候选 ${value.candidates} 个，识别 ${value.accepted} 个，重复 ${value.duplicates} 个，失败 ${value.failed} 个` : ''; };
    $('#scan-progress').textContent = job.status === 'discovering' ? '正在查找本地文件…' :
      job.status === 'analyzing' ? `正在识别候选素材：视频 ${processed.video || 0}/${totals.video || 0}，音乐 ${processed.music || 0}/${totals.music || 0}。完成后更新素材列表。` :
      job.status === 'completed' ? `扫描完成：${job.scan?.videos?.length || 0} 个视频、${job.scan?.music?.length || 0} 首音乐。${[summaryLine('video'), summaryLine('music')].filter(Boolean).join('；')}` : `扫描失败：${job.error || '未知错误'}`;
    const signature = JSON.stringify([job.id, job.status, job.scan?.videos?.length, job.scan?.music?.length]);
    if (job.scan && job.status === 'completed' && signature !== state.scanSignature) {
      state.scanSignature = signature;
      state.scan = job.scan;
      renderAssets();
      if (job.status === 'completed') renderErrors(job.scan.errors || []);
    }
    if (running) { stopScanPolling(); state.scanPoller = setTimeout(pollScan, 900); return; }
    stopScanPolling();
    if (job.status === 'completed') {
      const videoDir = $('#video-dir').value.trim(), musicDir = $('#music-dir').value.trim();
      if (job.video_dir === videoDir && job.music_dir === musicDir) { state.libraryDirs = {video: videoDir, music: musicDir}; state.dirtyLibrary.clear(); }
      $('#plan-btn').disabled = !!state.dirtyLibrary.size;
      toast(`扫描完成：${job.scan.videos.length} 个视频，${job.scan.music.length} 首音乐。`);
    } else if (job.status === 'failed') {
      api('/api/bootstrap').then(data => {
        state.scan = data.scan || {videos:[], music:[], errors:[]};
        renderAssets();
        $('#plan-btn').disabled = !!state.dirtyLibrary.size;
      }).catch(() => {});
      renderErrors([job.error || '素材扫描失败']);
      toast(job.error || '素材扫描失败', true);
    }
  }
  async function scan() {
    const video_dir = $('#video-dir').value.trim(), music_dir = $('#music-dir').value.trim();
    if (!video_dir || !music_dir) return toast('请先填写视频和音乐文件夹。', true);
    const videoChanged = !state.libraryDirs || state.dirtyLibrary.has('video') || video_dir !== state.libraryDirs.video;
    const musicChanged = !state.libraryDirs || state.dirtyLibrary.has('music') || music_dir !== state.libraryDirs.music;
    const kind = videoChanged && !musicChanged ? 'video' : musicChanged && !videoChanged ? 'music' : 'both';
    $('#scan-btn').disabled = true; renderErrors([]);
    try {
      const job = await api('/api/scan/start', {method:'POST', body:JSON.stringify({video_dir,music_dir,kind})});
      applyScanJob({...job, video_dir, music_dir});
    } catch (error) { $('#scan-btn').disabled = false; toast(error.message === '接口不存在' ? '旧版后台仍在运行，请停止后台并重新打开 MixCut Studio。' : error.message, true); }
  }
  async function createPlan() { let config; try { config = getConfig(); } catch (error) { toast(error.message, true); return; } const btn = $('#plan-btn'); btn.disabled = true; btn.textContent = '正在规划…'; try { const data = await api('/api/plan', { method: 'POST', body: JSON.stringify({ config }) }); const planned = data.batch || data; state.batches = [planned, ...state.batches.filter(batch => batch.id !== planned.id)]; renderPlan(planned); location.hash = 'plan'; toast('方案生成完成，请核对每条歌曲与视频区间。'); } catch (error) { renderPlan(null); $('#plan-message').textContent = `无法生成方案：${error.message}`; location.hash = 'plan'; toast(error.message, true); } finally { btn.disabled = false; btn.textContent = '生成方案'; } }
  async function batchAction(id, action) { try { const data = await api(`/api/batches/${encodeURIComponent(id)}/${action}`, { method: 'POST', body: '{}' }); if (action === 'start') renderPlan(null); else if (data.batch && state.batch?.id === id) renderPlan(data.batch); toast({start:'已加入渲染队列',pause:'将在执行中的成片完成后暂停',resume:'已继续队列',stop:'已停止队列',retry:'已安排重试'}[action] || '操作成功'); if (action === 'start') location.hash = 'tasks'; refreshBatches(); } catch (error) { toast(error.message, true); } }
  async function itemAction(batchId, itemId, action) { try { const result = await api(`/api/batches/${encodeURIComponent(batchId)}/items/${encodeURIComponent(itemId)}/${action}`, { method:'POST', body:'{}' }); if (action === 'delete' && result.updated_batches) applyDeletedBatches(result.updated_batches); else { state.batchSignature=''; await refreshBatches(); } toast(result.preview_cleanup_errors?.length ? `任务已删除，但部分缩略图未能清理：${result.preview_cleanup_errors[0]}` : {start:'已重新开始该条任务',stop:'已终止该条任务',delete:'已删除任务；成品和素材保留，缩略图已清理'}[action], !!result.preview_cleanup_errors?.length); } catch (error) { toast(error.message,true); } }
  async function allBatches(action) { const candidates=state.batches.filter(batch=>action==='pause'?['queued','running'].includes(batch.status):(action==='stop'?['queued','running','pausing','paused'].includes(batch.status):['paused','stopped','draft'].includes(batch.status))); for(const batch of candidates){const next=action==='pause'?'pause':(action==='stop'?'stop':(batch.status==='paused'?'resume':'start'));try{await api(`/api/batches/${encodeURIComponent(batch.id)}/${next}`,{method:'POST',body:'{}'});}catch(error){toast(`批次 ${batch.id}：${error.message}`,true);}} state.batchSignature='';await refreshBatches(); }
  function applyDeletedBatches(updatedBatches) {
    const byId = new Map(state.batches.map(batch => [batch.id, batch]));
    Object.entries(updatedBatches).forEach(([id, batch]) => { if (batch) byId.set(id, batch); else byId.delete(id); });
    state.batches = [...byId.values()].sort((a, b) => (b.created_at || 0) - (a.created_at || 0));
    state.batchSignature = batchesSignature(state.batches);
    state.batchLayoutSignature = batchesLayoutSignature(state.batches);
    const anchor = currentView() === 'tasks' ? taskScrollAnchor() : null;
    renderBatches(state.batches);
    hydrateGalleries();
    if (anchor) { restoreTaskScroll(anchor); requestAnimationFrame(() => restoreTaskScroll(anchor)); }
    if (state.batch && Object.prototype.hasOwnProperty.call(updatedBatches, state.batch.id)) {
      renderPlan(updatedBatches[state.batch.id]);
    }
    updateSelectionButtons();
  }
  async function deleteSelectedTasks(scope = 'tasks') {
    const batches = scope === 'plan' ? (state.batch ? [state.batch] : []) : state.batches;
    const selections = batches.map(batch => ({batch_id:batch.id,item_ids:(batch.items || []).filter(item => selectedTasks.has(taskKey(batch.id,item.id))).map(item => item.id)})).filter(entry => entry.item_ids.length);
    const items = batches.flatMap(batch => (batch.items || []).filter(item => selectedTasks.has(taskKey(batch.id,item.id))));
    if (!items.length) return toast('请先勾选任务。',true);
    const button = $(scope === 'plan' ? '#delete-selected-plan' : '#delete-selected-tasks');
    button.disabled = true; button.textContent = '删除中…';
    try {
      const result = await api('/api/tasks/forget',{method:'POST',body:JSON.stringify({selections})});
      selections.forEach(entry => entry.item_ids.forEach(id => selectedTasks.delete(taskKey(entry.batch_id,id))));
      if (result.updated_batches) applyDeletedBatches(result.updated_batches);
      else { state.batchSignature=''; await refreshBatches(); if (state.batch) renderPlan(state.batches.find(batch => batch.id === state.batch.id) || null); }
      if (scope === 'plan') $('#plan-message').textContent = `已删除 ${result.deleted_items} 条任务记录，当前方案剩余 ${state.batch?.items?.length || 0} 条。成品和素材保留，缩略图已清理。`;
      toast(result.preview_cleanup_errors?.length ? `任务已删除，但部分缩略图未能清理：${result.preview_cleanup_errors[0]}` : `已删除 ${result.deleted_items} 条任务记录；成品和素材保留，缩略图已清理。`, !!result.preview_cleanup_errors?.length);
    } catch(error) { toast(error.message,true); }
    finally { button.disabled = false; updateSelectionButtons(); }
  }
  function selectAllTasks(scope) {
    const batches = scope === 'plan' ? (state.batch ? [state.batch] : []) : state.batches;
    batches.forEach(batch => (batch.items || []).forEach(item => selectedTasks.add(taskKey(batch.id,item.id))));
    if (scope === 'plan') renderPlan(state.batch); else renderBatches(state.batches);
    updateSelectionButtons();
  }
  async function clearAllBatches() {
    try {
      const result=await api('/api/tasks/clear',{method:'POST',body:'{}'});
      selectedTasks.clear();applyDeletedBatches(result.updated_batches);renderPlan(null);
      toast(result.preview_cleanup_errors?.length ? `任务已清除，但部分缩略图未能清理：${result.preview_cleanup_errors[0]}` : `已清除 ${result.deleted_items} 条任务记录；成品和素材保留，缩略图已清理。`, !!result.preview_cleanup_errors?.length);
    } catch(error){toast(error.message,true);}
  }
  async function downloadDeletionRecords() {
    try {
      const records = await api('/api/deletion-records');
      if (!records.length) return toast('还没有删除记录。');
      const url = URL.createObjectURL(new Blob([JSON.stringify(records,null,2)],{type:'application/json'}));
      const link = document.createElement('a'); link.href=url; link.download='MixCut-deletion-records.json';
      document.body.append(link); link.click(); link.remove(); setTimeout(()=>URL.revokeObjectURL(url),1000);
    } catch(error) { toast(error.message,true); }
  }
  function renderBatches(batches) { const box = $('#batches'); box.replaceChildren(); if (!batches.length) { box.className = 'batch-list empty'; box.textContent = '尚无任务。先生成方案并确认开始渲染。'; $('#task-summary').replaceChildren(stat('等待批次', '—')); return; } box.className = 'batch-list'; const items = batches.flatMap(b => b.items || []); const counts = items.reduce((out, item) => { const k = safeClass(item.status); out[k] = (out[k] || 0) + 1; return out; }, {}); const summary = $('#task-summary'); summary.replaceChildren(stat('总条数', items.length), stat('完成', counts.completed || counts.success || 0), stat('处理中', (counts.running || 0) + (counts.processing || 0) + (counts.validating || 0)), stat('失败', counts.failed || counts.error || 0), stat('待处理', counts.pending || 0), stat('已取消', counts.cancelled || 0));
    const statusLabels = { draft:'草稿', queued:'排队中', running:'渲染中', pausing:'等待当前条完成', paused:'已暂停', stalled_paused:'执行被卡住暂停', stopping:'正在停止', stopped:'已停止', completed:'已完成', failed:'失败', pending:'待处理', success:'已完成', processing:'处理中', error:'失败', cancelled:'已取消' }; batches.forEach(batch => { const card = document.createElement('article'); card.className = 'batch'; card.dataset.batchId = batch.id; const batchStatus = safeClass(batch.status); const head = document.createElement('div'); head.className = 'batch-head'; const title = document.createElement('h3'); title.textContent = `批次 ${batch.id}`; const status = document.createElement('span'); status.className = `status ${batchStatus}`; status.textContent = statusLabels[batchStatus] || batch.status || '未知状态'; head.append(title, status); if(batch.schedule_name){const tag=document.createElement('small');tag.textContent=`定时任务：${batch.schedule_name}`;head.append(tag);} const all = batch.items?.length || 0, done = (batch.items || []).filter(i => ['completed','success'].includes(String(i.status).toLowerCase())).length, failed = (batch.items || []).some(i => ['failed','error'].includes(safeClass(i.status))); const progress = document.createElement('div'); progress.className = 'progress'; const bar = document.createElement('i'); bar.style.width = `${all ? Math.round(done / all * 100) : 0}%`; progress.append(bar); const controls = document.createElement('div'); controls.className = 'batch-controls'; const allowed = { pause:['queued','running'], resume:['paused'], stop:['queued','running','pausing','paused'], retry:['failed','stopped','completed'] }; [['pause','暂停（执行中任务完成后）'],['resume','继续'],['stop','停止'],['retry','重试失败项']].forEach(([action, label]) => { const button = document.createElement('button'); button.className = 'button'; button.textContent = label; button.disabled = !allowed[action].includes(batchStatus) || (action === 'retry' && !failed); button.onclick = () => batchAction(batch.id, action); controls.append(button); }); const view = document.createElement('button'); view.className = 'button'; view.textContent = '查看方案'; view.onclick = () => { renderPlan(batch); location.hash = 'plan'; }; controls.append(view); const reveal = document.createElement('button'); reveal.className = 'button'; reveal.textContent = '在 Finder 中显示'; reveal.onclick = async () => { try { await api('/api/reveal', { method:'POST', body:JSON.stringify({ batch_id: batch.id }) }); } catch (e) { toast(e.message, true); } }; controls.append(reveal);
    const reviewJob = state.reviewJobs.get(batch.id);
    const reviewAll = document.createElement('button'); reviewAll.className = 'button primary batch-approve-all'; reviewAll.type = 'button'; reviewAll.textContent = '一键审核'; reviewAll.disabled = reviewJob?.status === 'running' || !reviewCandidates(batch).length; reviewAll.onclick = () => approveBatch(batch); controls.append(reviewAll);
    const reviewNote = document.createElement('span'); reviewNote.className = `batch-review-state ${reviewJob?.status === 'failed' ? 'error' : 'field-help'}`; reviewNote.textContent = reviewJob?.status === 'running' ? `审核中 ${reviewJob.completed}/${reviewJob.total}，当前任务 ${reviewJob.current_item || '准备中'}` : reviewJob?.status === 'failed' ? `审核停在任务 ${reviewJob.failed_item}：${reviewJob.error}。修复后可再次点击。` : ''; controls.append(reviewNote);
    if (batch.error || batch.recovery_note) { const note = document.createElement('div'); note.className = 'notice'; note.textContent = batch.error || batch.recovery_note; card.append(note); } const taskItems = document.createElement('div'); taskItems.className = 'task-items'; (batch.items || []).forEach(item => { const row = document.createElement('div'); row.className = 'task-item'; row.dataset.taskKey = taskKey(batch.id, item.id); const index = document.createElement('span'); index.textContent = String(item.index ?? '—').padStart(2, '0'); const label = document.createElement('span'); label.className = 'task-label'; const pct = Number.isFinite(Number(item.progress)) ? ` · ${Math.round(Number(item.progress) * 100)}%` : ''; label.textContent = `${item.output_name ? item.output_name + ' · ' : ''}${fmt(item.duration)}${pct}${item.status === 'running' && item.render_stage === 'silence_detection' ? ` · 正在进行音乐静音检测（${item.music_detection?.done || 0}/${item.music_detection?.total || 0} 首）` : ''}`; const itemStatus = document.createElement('span'); const itemState = safeClass(item.status); itemStatus.className = `status ${itemState}`; itemStatus.textContent = itemState === 'success' && item.review?.status !== 'approved' && item.thumbnails ? ({queued:'缩略图排队中',generating:'生成缩略图',ready:'可审核',failed:'缩略图失败'}[item.thumbnails.status] || '已完成') : (statusLabels[itemState] || item.status || '待处理'); row.append(taskSelection(batch,item),index,label,itemStatus); if (item.status === 'success' && item.result?.shortened_seconds > 0.1) { const note = document.createElement('p'); note.className='field-help duration-note'; note.textContent=`实际时长约 ${fmt(item.result.duration)}，比规划短约 ${fmt(item.result.shortened_seconds)}；已按有效音乐时长缩短视频。`; row.append(note); } if (item.error) { const error = document.createElement('div'); error.className = 'error'; error.textContent = item.error; row.append(error); } if (['completed','success'].includes(String(item.status).toLowerCase()) && !item.cleanup?.output_deleted) { const video = document.createElement('video'); video.controls = true; video.preload = 'none'; video.src = `/api/output?batch=${encodeURIComponent(batch.id)}&item=${encodeURIComponent(item.id)}`; row.append(video); } taskItems.append(row); }); card.append(head, progress, controls, taskItems); box.append(card); });
  }
  async function refreshCache() {
    try { const data = await api('/api/cache'); $('#cache-usage').textContent = `剪辑缓存：${(data.bytes / 1024 ** 3).toFixed(2)} GB · ${data.files} 个文件`; }
    catch (error) { $('#cache-usage').textContent = `缓存读取失败：${error.message}`; }
  }
  async function clearCache() {
    const button = $('#clear-cache'); button.disabled = true;
    try { const data = await api('/api/cache/clear', {method:'POST',body:'{}'}); await refreshCache(); toast(data.errors.length ? `部分缓存未能删除：${data.errors[0]}` : data.active_tasks ? '已清除空闲缓存，正在使用的缓存暂时保留。' : '剪辑缓存已清除。', !!data.errors.length); }
    catch (error) { toast(error.message,true); } finally { button.disabled = false; }
  }
  async function retryFailed() {
    const button = $('#retry-failed'); button.disabled = true;
    try { const data = await api('/api/tasks/retry-failed', {method:'POST',body:'{}'}); await refreshBatches(); toast(`已安排 ${data.retried} 条失败任务重试。${data.errors.join('；')}`, !!data.errors.length); }
    catch (error) { toast(error.message,true); } finally { button.disabled = false; }
  }
  async function refreshBatches(silent = false) {
    if (!silent) refreshCache();
    if (silent && $$('video').some(video => !video.paused && !video.ended)) return;
    let data;
    try { data = await api('/api/batches'); }
    catch (error) { if (!silent) toast(error.message, true); setServer(false, '本地服务未连接'); return; }
    setServer(true, connectedLabel());
    const batches = data.batches || data || [];
    const signature = batchesSignature(batches);
    if (signature === state.batchSignature) { if (currentView() === 'tasks' && !state.poller) setupPolling(); return; }
    const layoutSignature = batchesLayoutSignature(batches);
    const sameLayout = layoutSignature === state.batchLayoutSignature;
    const anchor = sameLayout ? null : taskScrollAnchor();
    if (!sameLayout) captureCompletedMedia();
    state.batches = batches; state.batchSignature = signature; state.batchLayoutSignature = layoutSignature;
    if (sameLayout) updateTaskProgress(batches);
    else { renderBatches(batches); hydrateGalleries(); restoreTaskScroll(anchor); requestAnimationFrame(() => restoreTaskScroll(anchor)); }
    if (!state.poller) setupPolling();
  }
  async function bootstrap() { try { const data = await api('/api/bootstrap'); if (data.api_protocol !== 9) throw new Error(`本地后台接口版本不兼容（当前 ${data.api_protocol ?? '未知'}，需要 9）。请先停止旧版后台再重新打开 MixCut Studio。`); state.serverVersion = data.version || ''; $('#video-dir').value = data.video_dir || ''; $('#music-dir').value = data.music_dir || ''; state.libraryDirs = {video: data.video_dir || '', music: data.music_dir || ''}; $('#output-dir').value = data.output_dir || ''; syncReviewDir(data.review_dir || ''); state.scan = data.scan || state.scan; state.batches = data.batches || []; state.reviewJobs = new Map(Object.entries(data.review_jobs || {})); state.batchSignature = batchesSignature(state.batches); state.batchLayoutSignature = batchesLayoutSignature(state.batches); renderAssets(); updateStickerSelectors(); renderErrors(state.scan.errors || []); renderBatches(state.batches); refreshCache(); hydrateGalleries(); const draft = state.batches.find(batch => batch.status === 'draft'); if (draft) renderPlan(draft); if (data.scan_job && ['discovering','analyzing'].includes(data.scan_job.status)) applyScanJob(data.scan_job); setServer(true, data.ffmpeg_available === false ? `${connectedLabel()} · 未找到 FFmpeg` : connectedLabel()); Object.entries(data.review_jobs || {}).filter(([, job]) => job.status === 'running').forEach(([id]) => watchBatchReview(id)); if (data.global_review_job?.status === 'running') watchAllReview(); document.dispatchEvent(new Event('mixcut-bootstrap')); } catch (error) { setServer(false, error.message); renderErrors([error.message]); } }
  document.addEventListener('DOMContentLoaded', () => { window.addEventListener('hashchange', showView); $('#scan-btn').addEventListener('click', scan); $('#plan-btn').addEventListener('click', createPlan); $('#start-btn').addEventListener('click', () => state.batch && batchAction(state.batch.id, 'start')); $('#manifest-btn').addEventListener('click', () => { if (state.batch) window.open(`/api/manifest?batch=${encodeURIComponent(state.batch.id)}`, '_blank', 'noopener'); }); $('#clear-cache').addEventListener('click', clearCache); $('#retry-failed').addEventListener('click', retryFailed); $('#refresh-tasks').addEventListener('click', () => refreshBatches()); $('#pause-all').addEventListener('click',()=>allBatches('pause')); $('#start-all').addEventListener('click',()=>allBatches('start')); $('#stop-all').addEventListener('click',()=>allBatches('stop')); $('#approve-all-visible').addEventListener('click',approveAllVisible); $('#clear-all').addEventListener('click',clearAllBatches); $('#select-all-plan').addEventListener('click',()=>selectAllTasks('plan')); $('#delete-selected-plan').addEventListener('click',()=>deleteSelectedTasks('plan')); $('#select-all-tasks').addEventListener('click',()=>selectAllTasks('tasks')); $('#delete-selected-tasks').addEventListener('click',()=>deleteSelectedTasks('tasks')); $('#shutdown-btn').addEventListener('click', async () => { if (!confirm('确定停止本地后台服务吗？正在处理的任务会停止。')) return; try { await api('/api/shutdown', { method:'POST', body:'{}' }); } catch (error) { toast(error.message, true); return; } stopPolling(); setServer(false, '本地服务已停止'); toast('已请求停止后台服务。'); }); $('#config-form').addEventListener('change', event => { if (event.target.name === 'mode') { $$('.mode-multi').forEach(el => el.hidden = event.target.value !== 'multi'); $$('.mode-single').forEach(el => el.hidden = event.target.value !== 'single'); } if (event.target.name === 'music_mode') $$('.pool-settings').forEach(el => el.hidden = event.target.value !== 'pool'); }); $('#video-assets').addEventListener('change', event => { if (event.target.matches('.asset-select,.group-input')) { saveAssetPrefs(); renderPickers(); } }); $('#music-assets').addEventListener('change', event => { if (event.target.matches('.asset-select')) { saveAssetPrefs(); renderPickers(); } }); bootstrap().then(showView); });
  document.addEventListener('DOMContentLoaded', () => { $$('.folder-picker').forEach(button => button.addEventListener('click', () => pickFolder(button.dataset.kind))); $('#output-dir').addEventListener('change', () => persistPreference('output_dir', $('#output-dir').value.trim())); ['review-dir', 'task-review-dir'].forEach(id => $(`#${id}`).addEventListener('change', () => { const value = $(`#${id}`).value.trim(); syncReviewDir(value); persistPreference('review_dir', value); })); [['video-dir','video'],['music-dir','music']].forEach(([id,kind]) => $(`#${id}`).addEventListener('change', () => { if ($(`#${id}`).value.trim() !== state.libraryDirs?.[kind]) clearScannedLibrary(kind); })); });
  const polishTaskControls = () => $$('.batch').forEach(card => {
    const badge = $('.batch-head .status', card); if (badge?.textContent === 'validating') badge.textContent = '校验中';
    const batchId = $('.batch-head h3', card)?.textContent.replace('批次 ', ''); const batch = state.batches.find(item => String(item.id) === batchId);
    if (batch?.output_folder && !$('.output-location', card)) { const output = document.createElement('p'); output.className = 'output-location field-help'; output.textContent = `输出目录：${batch.output_folder}`; $('.batch-head', card).after(output); }
    const hasFailed = $$('.task-item .status', card).some(el => ['failed', 'error', '失败'].includes(safeClass(el.textContent)) || el.textContent === '失败'); const hasPending = $$('.task-item .status', card).some(el => el.textContent === '待处理'); const stateText = badge?.textContent;
    $$('.batch-controls .button', card).forEach(button => { if (button.textContent === '重试失败项' && hasFailed && stateText === '已暂停') button.disabled = false; if (button.textContent === '继续' && hasPending && stateText === '已停止') button.disabled = false; });
    $$('.task-item', card).forEach((row,index) => { if ($('.item-controls',row)) return; const item=batch?.items?.[index]; if(!item)return; const controls=document.createElement('div');controls.className='item-controls'; if(['pending','running','validating'].includes(item.status)){const stop=document.createElement('button');stop.className='button';stop.textContent='终止该条';stop.onclick=()=>itemAction(batch.id,item.id,'stop');controls.append(stop);} if(['cancelled','failed','stalled_paused'].includes(item.status)){const start=document.createElement('button');start.className='button';start.textContent=['failed','stalled_paused'].includes(item.status)?'重试该任务':'开始该条';start.onclick=()=>itemAction(batch.id,item.id,'start');controls.append(start);} if(item.status==='success' && (!item.thumbnails || item.thumbnails.status==='ready')){const details=document.createElement('button');details.className='button';details.textContent='查看与审核';details.onclick=()=>{row.classList.toggle('expanded');details.textContent=row.classList.contains('expanded')?'收起详情':'查看与审核';};controls.append(details);} const remove=document.createElement('button');remove.className='button';remove.textContent='删除任务';remove.onclick=()=>itemAction(batch.id,item.id,'delete');controls.append(remove);row.append(controls); });
  });
  new MutationObserver(polishTaskControls).observe($('#batches'), { childList: true, subtree: true });
  const polishOutputFolders = () => $$('.batch').forEach(card => { const batchId = $('.batch-head h3', card)?.textContent.replace('批次 ', ''); const batch = state.batches.find(item => String(item.id) === batchId); const output = $('.output-location', card); const text = batch && `批次 ${batch.folder_name || batch.id} · 输出目录：${batch.output_folder}`; if (output && text && output.textContent !== text) output.textContent = text; });
  new MutationObserver(polishOutputFolders).observe($('#batches'), { childList: true, subtree: true });
  const polishReviewActions = () => $$('.batch').forEach(card => {
    const batchId = $('.batch-head h3', card)?.textContent.replace('批次 ', '');
    const batch = state.batches.find(entry => String(entry.id) === batchId);
    if (!batch) return;
    $$('.task-item', card).forEach((row,index) => {
      if ($('.review-action', row)) return;
      const video = $('video', row);
      const item = (batch.items || [])[index];
      if (!item) return;
      if (item.status !== 'success') return;
      if (item.thumbnails && item.thumbnails.status !== 'ready' && item.review?.status !== 'approved') {
        const note = document.createElement('div'); note.className = 'review-action preview-status';
        note.textContent = item.thumbnails.status === 'failed' ? `缩略图生成失败：${item.thumbnails.error || '请重试'}` : '视频已完成，缩略图准备中；全部就绪后可审核。';
        if (item.thumbnails.status === 'failed') {
          const retry = document.createElement('button'); retry.className = 'button'; retry.textContent = '重试生成缩略图';
          retry.onclick = async () => { try { await api(`/api/batches/${batch.id}/items/${item.id}/prepare-previews`, {method:'POST',body:'{}'}); await refreshBatches(); } catch(error) { toast(error.message,true); } };
          note.append(retry);
        }
        row.append(note); return;
      }
      const action = document.createElement('div');
      action.className = 'review-action field-help';
      if (item.review?.status === 'approved') {
        action.classList.add('review-approved');
        action.textContent = `已通过审核：${item.review.path || '已归档'}`;
        const reveal = document.createElement('button');
        reveal.className = 'button'; reveal.type = 'button';
        reveal.textContent = '在 Finder 中显示审核文件';
        reveal.addEventListener('click', async () => {
          try { await api('/api/reveal', { method:'POST', body:JSON.stringify({batch_id:batch.id,item_id:item.id,review:true}) }); }
          catch (error) { toast(error.message,true); }
        });
        action.append(document.createElement('br'),reveal);
        const cleanup = document.createElement('p'); cleanup.className = 'field-help';
        const sourceStates = Object.values(item.cleanup?.source_files || {});
        cleanup.textContent = `制作目录成品：${item.cleanup?.output_deleted ? '已删除' : (item.cleanup?.output_error || '待确认')} · 原始视频：${sourceStates.length ? sourceStates.join('；') : '无待删源片段记录'}`;
        const origin = batch.sticker_origin;
        const original = item.kind === 'sticker_variant' && origin ? (state.batches.find(entry => entry.id === origin.batch_id)?.items || []).find(entry => entry.id === origin.item_id) : null;
        if (item.kind === 'sticker_variant') {
          cleanup.textContent += ` · 贴图前成片：${item.cleanup?.origin_status || (item.cleanup?.origin_output_deleted ? '已删除' : (item.cleanup?.origin_error || '待清理'))}`;
          if (original?.segments?.length) cleanup.textContent += ` · 原素材：${Object.values(original.cleanup?.source_files || {}).join('；') || '待清理'}`;
        }
        action.append(cleanup);
        if (!item.cleanup?.output_deleted || (item.segments?.length && !item.cleanup?.original_recordings_deleted) || (item.kind === 'sticker_variant' && (!item.cleanup?.origin_output_deleted || (original?.segments?.length && !original.cleanup?.original_recordings_deleted)))) {
          const retry = document.createElement('button'); retry.className = 'button'; retry.type = 'button'; retry.textContent = '重试清理原片与制作成片';
          retry.addEventListener('click', async () => { retry.disabled = true; try { await api(`/api/batches/${encodeURIComponent(batch.id)}/items/${encodeURIComponent(item.id)}/retry-cleanup`, {method:'POST',body:'{}'}); state.batchSignature=''; await refreshBatches(); toast('清理状态已更新。'); } catch(error) { retry.disabled = false; toast(error.message,true); } });
          action.append(retry);
        }
      } else if (item.review?.status === 'superseded') {
        action.textContent = `已由贴图版本替代：${item.review.replacement_batch_id || '审核版'}`;
        const sourceStates = Object.values(item.cleanup?.source_files || {});
        const cleanup = document.createElement('p'); cleanup.className = 'field-help';
        cleanup.textContent = `原成片：${item.cleanup?.output_deleted ? '已删除' : (item.cleanup?.output_error || '待清理')} · 原始视频：${sourceStates.length ? sourceStates.join('；') : '无待删源片段记录'}`;
        action.append(cleanup);
      } else {
        const approve = document.createElement('button');
        approve.className = 'button primary'; approve.type = 'button';
        approve.disabled = item.review?.status === 'copying';
        approve.textContent = approve.disabled ? '正在归档' : '通过审核';
        approve.addEventListener('click', () => approveItem(batch.id,item.id,approve));
        action.append(approve);
        const variant = document.createElement('select'); variant.className = 'sticker-variant'; variant.setAttribute('aria-label','审核时追加的贴图模板'); variant.append(new Option('不追加贴图', '')); (state.stickers?.templates || []).forEach(template => variant.append(new Option(template.name, template.id))); const makeVariant = document.createElement('button'); makeVariant.className = 'button'; makeVariant.type = 'button'; makeVariant.textContent = '生成贴图版本'; makeVariant.addEventListener('click', async () => { if (!variant.value) return toast('请选择一个贴图模板。', true); makeVariant.disabled = true; try { const job = await api('/api/sticker-variants', { method:'POST', body:JSON.stringify({batch_id:batch.id,item_id:item.id,template_id:variant.value}) }); makeVariant.textContent = '贴图版本排队中'; const timer = setInterval(async () => { try { const status = await api(`/api/sticker-variants/${encodeURIComponent(job.job_id)}`); makeVariant.textContent = status.status === 'completed' ? '贴图版本已生成' : `生成中 ${Math.round((status.progress || 0) * 100)}%`; if (['completed','failed'].includes(status.status)) { clearInterval(timer); makeVariant.disabled = false; if (status.status === 'failed') toast(status.error || '贴图版本生成失败', true); await refreshBatches(); } } catch (_) { clearInterval(timer); makeVariant.disabled = false; } }, 3000); } catch (error) { makeVariant.disabled = false; toast(error.message,true); } }); const hint=document.createElement('p');hint.className='field-help';hint.textContent='审核贴图版后，会清理未审核的原成片及可安全删除的原素材。';makeVariant.disabled=!variant.value;variant.addEventListener('change',()=>{makeVariant.disabled=!variant.value;approve.disabled=!!variant.value || item.review?.status==='copying';approve.textContent=variant.value?'请先生成贴图版本':(approve.disabled?'正在归档':'通过审核');});action.append(hint, variant, makeVariant);
        if (item.review?.error) {
          const error = document.createElement('div'); error.className = 'review-error';
          error.textContent = `归档失败：${item.review.error}`; action.append(error);
        }
      }
      if (video) { const directSticker = document.createElement('button'); directSticker.className = 'button direct-sticker'; directSticker.type = 'button'; directSticker.textContent = '在视频上添加贴图'; directSticker.addEventListener('click', () => openVideoStickerEditor(batch, item, video)); action.append(document.createElement('br'), directSticker); video.before(action); }
      else row.append(action);
    });
  });
  new MutationObserver(polishReviewActions).observe($('#batches'), { childList: true, subtree: true });
  state.stickers = { assets: [], templates: [], active: null, layers: [] };
  const stickerImage = id => `/api/sticker?id=${encodeURIComponent(id)}`;
  const waitForStickerVariant = (job, button, dialog) => {
    button.textContent = '贴图版本排队中';
    const timer = setInterval(async () => {
      try {
        const status = await api(`/api/sticker-variants/${encodeURIComponent(job.job_id)}`);
        button.textContent = status.status === 'completed' ? '贴图版本已生成' : `生成中 ${Math.round((status.progress || 0) * 100)}%`;
        if (['completed', 'failed'].includes(status.status)) {
          clearInterval(timer); button.disabled = false;
          if (status.status === 'failed') toast(status.error || '贴图版本生成失败', true);
          else { dialog?.close(); toast('贴图版本已生成；审核通过后将安全清理原成片和原素材。'); }
          await refreshBatches();
        }
      } catch (_) { clearInterval(timer); button.disabled = false; }
    }, 3000);
  };
  const openVideoStickerEditor = (batch, item, sourceVideo) => {
    if (!state.stickers.assets.length) return toast('贴图库为空，请先到“贴图库”导入图片或 GIF。', true);
    const layers = [];
    const dialog = document.createElement('dialog'); dialog.className = 'video-sticker-dialog';
    const heading = document.createElement('div'); heading.className = 'video-sticker-heading';
    const title = document.createElement('h2'); title.textContent = '在当前视频上添加贴图';
    const close = document.createElement('button'); close.className = 'button'; close.textContent = '关闭'; close.onclick = () => dialog.close();
    heading.append(title, close);
    const stage = document.createElement('div'); stage.className = 'video-sticker-stage';
    const video = document.createElement('video'); video.src = sourceVideo.src; video.controls = true; video.preload = 'metadata';
    if (Number.isFinite(sourceVideo.currentTime)) video.addEventListener('loadedmetadata', () => { video.currentTime = sourceVideo.currentTime; }, {once:true});
    stage.append(video);
    const controls = document.createElement('div'); controls.className = 'video-sticker-controls';
    const picker = document.createElement('select'); state.stickers.assets.forEach(asset => picker.append(new Option(asset.name, asset.id)));
    const add = document.createElement('button'); add.className = 'button'; add.textContent = '添加贴图';
    const layerList = document.createElement('div'); layerList.className = 'video-sticker-layers';
    const render = () => {
      $$('.placed-sticker', stage).forEach(image => image.remove()); layerList.replaceChildren();
      layers.forEach((layer, index) => {
        const asset = state.stickers.assets.find(entry => entry.id === layer.sticker_id); if (!asset) return;
        const image = document.createElement('img'); image.className = 'placed-sticker'; image.src = stickerImage(asset.id); image.draggable = false;
        const paint = () => { image.style.left = `${layer.x * 100}%`; image.style.top = `${layer.y * 100}%`; image.style.width = `${layer.width * 100}%`; image.style.opacity = layer.opacity; };
        paint(); stage.append(image);
        image.addEventListener('pointerdown', event => {
          event.preventDefault(); image.setPointerCapture(event.pointerId);
          const box = stage.getBoundingClientRect(), startX = event.clientX, startY = event.clientY, x = layer.x, y = layer.y;
          const move = next => { layer.x = Math.max(0, Math.min(1 - layer.width, x + (next.clientX - startX) / box.width)); layer.y = Math.max(0, Math.min(.99, y + (next.clientY - startY) / box.height)); paint(); };
          const done = () => { image.removeEventListener('pointermove', move); image.removeEventListener('pointerup', done); image.removeEventListener('pointercancel', done); };
          image.addEventListener('pointermove', move); image.addEventListener('pointerup', done); image.addEventListener('pointercancel', done);
        });
        const row = document.createElement('div'); row.className = 'video-sticker-layer';
        const name = document.createElement('span'); name.textContent = `${index + 1}. ${asset.name}`;
        const width = document.createElement('input'); width.type = 'range'; width.min = '5'; width.max = '80'; width.value = String(layer.width * 100); width.title = '贴图大小'; width.oninput = () => { layer.width = Number(width.value) / 100; layer.x = Math.min(layer.x, 1 - layer.width); paint(); };
        const opacity = document.createElement('input'); opacity.type = 'range'; opacity.min = '10'; opacity.max = '100'; opacity.value = String(layer.opacity * 100); opacity.title = '透明度'; opacity.oninput = () => { layer.opacity = Number(opacity.value) / 100; paint(); };
        const remove = document.createElement('button'); remove.className = 'button'; remove.textContent = '移除'; remove.onclick = () => { layers.splice(index, 1); render(); };
        row.append(name, document.createTextNode('大小'), width, document.createTextNode('透明度'), opacity, remove); layerList.append(row);
      });
    };
    add.onclick = () => { if (layers.length >= 8) return toast('每条视频最多添加 8 张贴图。', true); layers.push({sticker_id:picker.value,x:.05,y:.05,width:.2,opacity:1,start:0,end:null}); render(); };
    const save = document.createElement('button'); save.className = 'button primary'; save.textContent = '生成贴图版本';
    save.onclick = async () => { if (!layers.length) return toast('请先添加贴图。', true); save.disabled = true; try { const job = await api('/api/sticker-variants', {method:'POST',body:JSON.stringify({batch_id:batch.id,item_id:item.id,name:'视频内手动贴图',layers})}); waitForStickerVariant(job, save, dialog); } catch (error) { save.disabled = false; toast(error.message, true); } };
    const help = document.createElement('p'); help.className = 'field-help'; help.textContent = '直接拖动贴图改变位置；滑块调整大小和透明度。播放或拖动视频可检查不同画面。';
    controls.append(picker, add, layerList, save, help); dialog.append(heading, stage, controls); document.body.append(dialog);
    dialog.addEventListener('close', () => dialog.remove()); dialog.showModal();
  };
  const updateStickerSelectors = () => {
    [$('#generation-sticker-template'), ...$$('.sticker-variant')].forEach(select => {
      const previous = select.value;
      select.replaceChildren(new Option(select.id ? '不添加贴图' : '不追加贴图', ''));
      state.stickers.templates.forEach(t => select.append(new Option(t.name, t.id)));
      select.value = state.stickers.templates.some(t => t.id === previous) ? previous : '';
      if(select.classList.contains('sticker-variant'))select.dispatchEvent(new Event('change'));
    });
    const backgrounds = $('#sticker-background');
    if (backgrounds) {
      const previous = backgrounds.value;
      backgrounds.replaceChildren(new Option('空白画布（16:9）', ''));
      state.batches.forEach(b => (b.items || []).filter(i => i.status === 'success').forEach(i => {
        backgrounds.append(new Option(`${b.folder_name || b.id} · 视频 ${i.index}`, `${b.id}:${i.id}`));
      }));
      if ([...backgrounds.options].some(o => o.value === previous)) backgrounds.value = previous;
    }
  };
  const addStickerLayer = id => {
    if (state.stickers.layers.length >= 8) return toast('一个模板最多8层贴图。', true);
    if (!id) return toast('请先导入贴图。', true);
    state.stickers.layers.push({sticker_id:id,x:.05,y:.05,width:.2,opacity:1,start:0,end:null});
    renderStickerEditor();
  };
  const renderStickerCatalog = () => {
    const assets = $('#sticker-assets'), templates = $('#sticker-templates');
    assets.replaceChildren(); templates.replaceChildren();
    state.stickers.assets.forEach(asset => {
      const row = document.createElement('button'); row.className='pick-row'; row.type='button';
      const image = document.createElement('img'); image.className='sticker-thumb'; image.src=stickerImage(asset.id); image.alt=asset.name;
      const name = document.createElement('span'); name.textContent=asset.name;
      row.append(image,name); row.onclick=()=>addStickerLayer(asset.id); assets.append(row);
    });
    state.stickers.templates.forEach(template => {
      const button=document.createElement('button'); button.className='pick-row'; button.type='button'; button.textContent=template.name;
      button.onclick=()=>{state.stickers.active=template;state.stickers.layers=structuredClone(template.layers);$('#template-name').value=template.name;renderStickerEditor();};
      templates.append(button);
    });
    if (!state.stickers.assets.length) assets.textContent='尚未导入贴图（PNG、JPG、WEBP、动态GIF，单文件最大12MB）。';
    if (!state.stickers.templates.length) templates.textContent='尚无模板；点击贴图可将它放入画布。';
    updateStickerSelectors();
  };
  const renderStickerEditor = () => {
    const stage=$('#sticker-stage'), rows=$('#sticker-layers');
    stage.replaceChildren(); rows.replaceChildren();
    state.stickers.layers.forEach((layer,index)=>{
      const asset=state.stickers.assets.find(a=>a.id===layer.sticker_id); if(!asset)return;
      const aspect=asset.width_px/asset.height_px;
      const effective=()=>Math.max(0,Math.min(layer.width,1-layer.x,(1-layer.y)*aspect/(16/9)));
      const image=document.createElement('img'); image.src=stickerImage(asset.id); image.alt=`画布贴图 ${index+1}：${asset.name}`; image.draggable=false; image.style.touchAction='none';
      const paint=()=>{image.style.left=`${layer.x*100}%`;image.style.top=`${layer.y*100}%`;image.style.width=`${effective()*100}%`;image.style.opacity=layer.opacity;};
      paint(); stage.append(image);
      image.addEventListener('pointerdown',event=>{
        event.preventDefault(); image.setPointerCapture(event.pointerId);
        const box=stage.getBoundingClientRect(), sx=event.clientX, sy=event.clientY, ox=layer.x, oy=layer.y, w=effective();
        const move=e=>{layer.x=Math.max(0,Math.min(1-w,ox+(e.clientX-sx)/box.width));layer.y=Math.max(0,Math.min(1-w*(16/9)/aspect,oy+(e.clientY-sy)/box.height));paint();};
        const finish=()=>{image.removeEventListener('pointermove',move);image.removeEventListener('pointerup',finish);image.removeEventListener('pointercancel',finish);renderStickerEditor();};
        image.addEventListener('pointermove',move);image.addEventListener('pointerup',finish);image.addEventListener('pointercancel',finish);
      });
      const row=document.createElement('div');row.className='layer-row';
      const assetLabel=document.createElement('label');assetLabel.textContent=`贴图层 ${index+1}`;
      const picker=document.createElement('select');
      state.stickers.assets.forEach(a=>picker.append(new Option(a.name,a.id,false,a.id===layer.sticker_id)));
      picker.onchange=()=>{layer.sticker_id=picker.value;renderStickerEditor();};assetLabel.append(picker);row.append(assetLabel);
      [['x','横向位置 %',true,0,99.99],['y','纵向位置 %',true,0,99.99],['width','宽度 %',true,1,100],['opacity','不透明度',false,0,1],['start','出现时间（秒）',false,0,null],['end','结束时间（留空至结尾）',false,0,null]].forEach(([key,label,percent,min,max])=>{
        const wrap=document.createElement('label');wrap.textContent=label;
        const input=document.createElement('input');input.type='number';input.step='.01';input.min=String(min);if(max!==null)input.max=String(max);
        input.value=layer[key]==null?'':String(percent?Math.round(layer[key]*10000)/100:layer[key]);
        input.oninput=()=>{
          if(key==='end'&&input.value==='')layer.end=null;
          else {let number=Number(input.value);if(!Number.isFinite(number))return;number=Math.max(min,number);if(max!==null)number=Math.min(max,number);layer[key]=percent?number/100:number;}
          paint();
        };
        input.onchange=()=>{input.oninput();renderStickerEditor();};
        wrap.append(input);row.append(wrap);
      });
      const remove=document.createElement('button');remove.type='button';remove.textContent='移除此层';remove.onclick=()=>{state.stickers.layers.splice(index,1);renderStickerEditor();};row.append(remove);rows.append(row);
    });
    if(!state.stickers.layers.length)rows.textContent='从贴图库点击一张图片，或点击“添加贴图层”。';
  };
  const loadStickers=async()=>{
    try {const catalog=await api('/api/stickers');state.stickers.assets=catalog.assets||[];state.stickers.templates=catalog.templates||[];renderStickerCatalog();renderStickerEditor();}
    catch(error){$('#sticker-assets').textContent=`无法读取贴图库：${error.message}`;}
  };
  document.addEventListener('DOMContentLoaded',()=>{
    loadStickers();
    window.addEventListener('hashchange',()=>{if(currentView()==='stickers')updateStickerSelectors();});
    $('#new-template').onclick=()=>{state.stickers.active=null;state.stickers.layers=[];$('#template-name').value='未命名模板';renderStickerEditor();};
    $('#add-layer').onclick=()=>addStickerLayer(state.stickers.assets[0]?.id);
    $('#sticker-background').onchange=event=>{
      const value=event.target.value, stage=$('#sticker-stage');
      if(!value){stage.style.backgroundImage='';stage.style.backgroundSize='';stage.style.backgroundRepeat='';return;}
      const [batch,item]=value.split(':');
      stage.style.backgroundSize='cover';stage.style.backgroundRepeat='no-repeat';
      stage.style.backgroundImage=`url("/api/keyframe?batch=${encodeURIComponent(batch)}&item=${encodeURIComponent(item)}&index=0&size=large")`;
    };
    $('#save-template').onclick=async()=>{
      const button=$('#save-template');button.disabled=true;
      try {
        const result=await api('/api/sticker-templates',{method:'POST',body:JSON.stringify({id:state.stickers.active?.id,name:$('#template-name').value.trim(),layers:state.stickers.layers})});
        state.stickers.assets=result.assets;state.stickers.templates=result.templates;state.stickers.active=result.template;
        state.stickers.layers=structuredClone(result.template.layers);renderStickerCatalog();renderStickerEditor();toast('模板已保存，可在生成或审核时选择。');
      }catch(error){toast(error.message,true);}finally{button.disabled=false;}
    };
    $('#sticker-upload').onchange=async event=>{
      for(const file of event.target.files){
        if(file.size>12*1024*1024){toast(`${file.name} 超过12MB`,true);continue;}
        try{
          const data=await new Promise((resolve,reject)=>{const r=new FileReader();r.onload=()=>resolve(String(r.result).split(',')[1]);r.onerror=reject;r.readAsDataURL(file);});
          const catalog=await api('/api/stickers/import',{method:'POST',body:JSON.stringify({name:file.name,data})});
          state.stickers.assets=catalog.assets;state.stickers.templates=catalog.templates;renderStickerCatalog();
        }catch(error){toast(error.message||'贴图读取失败',true);}
      }
      event.target.value='';
    };
  });
  // Scheduler UI intentionally owns one view-only poller; it never creates production runs by itself.
  (() => {
    const scheduleStatus = { waiting: '等待素材', running: '生成中', completed: '已完成', superseded: '已被新定时任务替代', cancelled: '已取消' };
    const localTime = value => value == null ? '—' : new Date(Number(value) * 1000).toLocaleString('zh-CN', { hour12: false });
    const localInput = value => {
      const date = new Date(Number(value) * 1000);
      date.setMinutes(date.getMinutes() - date.getTimezoneOffset());
      return date.toISOString().slice(0, 16);
    };
    const defaultStart = () => {
      const date = new Date(Date.now() + 10 * 60 * 1000);
      date.setMinutes(date.getMinutes() - date.getTimezoneOffset());
      return date.toISOString().slice(0, 16);
    };
    const configSummary = config => {
      const dimensions = config?.width && config?.height ? `${config.width}×${config.height}` : '默认尺寸';
      return `${config?.mode === 'multi' ? '多源拼接' : '单源连续'} · ${Number(config?.min_source_duration_minutes || 0) > 0 ? '源视频 > ' + config.min_source_duration_minutes + ' 分钟' : '源视频不限时长'} · ${dimensions} · ${config?.fps || 30} fps · ${config?.allow_overlap ? '允许画面重叠' : '不允许画面重叠'}${config?.within_group ? ' · 仅组内拼接' : ''}`;
    };
    const runSummary = run => {
      const status = scheduleStatus[run.status] || run.status || '未知';
      const detail = [`${run.name} · ${status}`, `已完成 ${run.completed_count || 0}/${run.target_count || 0}`];
      if (run.reason) detail.push(run.reason);
      if (run.last_error) detail.push(`错误：${run.last_error}`);
      if (run.next_check != null) detail.push(`下次检查：${localTime(run.next_check)}`);
      return detail.join(' · ');
    };
    const configFromCurrentForm = () => {
      const form = $('#config-form');
      const config = Object.fromEntries(new FormData(form));
      ['count', 'min_source_duration_minutes', 'min_duration', 'max_duration', 'min_songs', 'max_songs', 'start_gap', 'segment_min', 'segment_max', 'seed', 'fps', 'original_volume', 'music_volume'].forEach(key => { config[key] = Number(config[key]); });
      [config.width, config.height] = String(config.resolution || '1280x720').split('x').map(Number);
      delete config.resolution;
      if (String(config.seed || '').trim()) config.seed = Number(config.seed); else delete config.seed; config.allow_overlap = form.elements.allow_overlap.checked;
      config.within_group = config.within_group === 'true';
      config.sticker_template_id = config.sticker_template_id || null;
      config.first_song_ids = [];
      delete config.video_ids;
      delete config.music_ids;
      return config;
    };
    const scheduleCard = (schedule, onEdit, activeRun) => {
      const card = document.createElement('article');
      card.className = 'batch';
      const title = document.createElement('h3');
      title.textContent = schedule.name;
      const info = document.createElement('p');
      info.className = 'field-help';
      info.textContent = `${schedule.enabled ? '已启用' : '已停用'} · ${schedule.repeat === 'once' ? '仅一次' : schedule.repeat === 'daily' ? '每天' : '每周'} · 开始：${localTime(schedule.start_at)} · 下次：${localTime(schedule.next_due)} · 目标 ${schedule.target_count} 条 · 每 ${schedule.check_interval_minutes} 分钟检查`;
      const paths = document.createElement('p');
      paths.className = 'field-help';
      paths.textContent = `视频：${schedule.video_dir}；音乐：${schedule.music_dir}；导出：${schedule.output_dir}`;
      const snapshot = document.createElement('p');
      snapshot.className = 'field-help';
      snapshot.textContent = `剪辑快照：${configSummary(schedule.config)}`;
      const controls = document.createElement('div');
      controls.className = 'batch-controls';
      const ownsActive = activeRun?.schedule_id === schedule.id && ['waiting','running'].includes(activeRun.status);
      [['enable', '启用', schedule.enabled], ['disable', '停用并取消本轮', !schedule.enabled && !ownsActive], ['run-now', '立即启动', false]].forEach(([action, label, disabled]) => {
        const button = document.createElement('button');
        button.className = 'button'; button.type = 'button'; button.textContent = label; button.disabled = disabled;
        button.addEventListener('click', async () => {
          try { renderSchedules(await api(`/api/schedules/${encodeURIComponent(schedule.id)}/${action}`, { method: 'POST', body: '{}' })); }
          catch (error) { toast(error.message, true); }
        });
        controls.append(button);
      });
      const edit = document.createElement('button');
      edit.className = 'button'; edit.type = 'button'; edit.textContent = '编辑'; edit.addEventListener('click', () => onEdit(schedule)); controls.append(edit);
      card.append(title, info, paths, snapshot, controls);
      return card;
    };
    const renderSchedules = data => {
      const active = $('#schedule-active'), list = $('#schedules-list');
      active.textContent = data.active_run ? runSummary(data.active_run) : '暂无运行中的定时任务。';
      list.replaceChildren();
      const schedules = data.schedules || [];
      list.className = 'batch-list';
      const runs = (data.runs || []).filter(run => ['completed', 'superseded', 'cancelled'].includes(run.status));
      if (!schedules.length && !runs.length) {
        list.className = 'batch-list empty'; list.textContent = '尚无定时任务。'; return;
      }
      if (!schedules.length) {
        const empty = document.createElement('p'); empty.className = 'field-help'; empty.textContent = '尚无保存的定时任务。'; list.append(empty);
      } else schedules.forEach(schedule => list.append(scheduleCard(schedule, loadSchedule, data.active_run)));
      if (runs.length) {
        const heading = document.createElement('h2'); heading.textContent = '历史运行'; list.append(heading);
        runs.forEach(run => {
          const item = document.createElement('article'); item.className = 'batch';
          const title = document.createElement('h3'); title.textContent = `${run.name} · ${scheduleStatus[run.status] || run.status}`;
          const detail = document.createElement('p'); detail.className = 'field-help'; detail.textContent = `完成 ${run.completed_count || 0}/${run.target_count || 0} · ${run.reason || '无额外说明'}${run.last_error ? ` · 错误：${run.last_error}` : ''}`;
          item.append(title, detail); list.append(item);
        });
      }
    };
    let poller = null;
    let requestInFlight = false;
    const refreshSchedules = async () => {
      if (requestInFlight) return;
      requestInFlight = true;
      try { renderSchedules(await api('/api/schedules')); }
      catch (error) { $('#schedules-list').className = 'batch-list empty'; $('#schedules-list').textContent = `无法读取定时任务：${error.message}`; }
      finally { requestInFlight = false; }
    };
    const syncCurrentPaths = () => {
      const form = $('#schedule-form');
      if (!$('#schedule-current').checked || form.dataset.editing === 'true') return;
      $('#schedule-video-dir').value = $('#video-dir').value;
      $('#schedule-music-dir').value = $('#music-dir').value;
      $('#schedule-output-dir').value = $('#output-dir').value;
    };
    const loadSchedule = schedule => {
      const form = $('#schedule-form');
      form.dataset.scheduleId = schedule.id;
      form.dataset.editing = 'true';
      form.dataset.config = JSON.stringify(schedule.config || {});
      form.elements.name.value = schedule.name || '';
      form.elements.start_at.value = localInput(schedule.start_at);
      form.elements.repeat.value = schedule.repeat || 'once';
      form.elements.target_count.value = schedule.target_count || 50;
      form.elements.check_interval_minutes.value = schedule.check_interval_minutes || 10;
      form.elements.enabled.checked = !!schedule.enabled;
      $('#schedule-video-dir').value = schedule.video_dir || '';
      $('#schedule-music-dir').value = schedule.music_dir || '';
      $('#schedule-output-dir').value = schedule.output_dir || '';
      $('#schedule-current').checked = false;
      form.scrollIntoView({ behavior: 'smooth', block: 'start' });
    };
    const resetSchedule = () => {
      const form = $('#schedule-form');
      form.reset();
      delete form.dataset.scheduleId; delete form.dataset.config; delete form.dataset.editing;
      form.elements.name.value = '素材持续监测'; form.elements.start_at.value = defaultStart();
      form.elements.target_count.value = 50; form.elements.check_interval_minutes.value = 10;
      $('#schedule-current').checked = true;
      syncCurrentPaths();
    };
    const pickScheduleFolder = async kind => {
      const input = $(`#schedule-${kind}-dir`);
      const buttons = $$('.schedule-folder-picker'); buttons.forEach(button => button.disabled = true);
      try {
        const result = await api('/api/pick-folder', { method: 'POST', body: JSON.stringify({ kind, current_path: input.value.trim(), persist: false }) });
        if (result.cancelled || !result.path) return;
        input.value = result.path; $('#schedule-current').checked = false;
      } catch (error) { toast(error.message, true); }
      finally { buttons.forEach(button => button.disabled = false); }
    };
    const schedulerViewChanged = () => {
      if (currentView() !== 'schedules') { if (poller) clearInterval(poller); poller = null; return; }
      syncCurrentPaths(); refreshSchedules();
      if (!poller) poller = setInterval(() => { if (currentView() === 'schedules') refreshSchedules(); }, 3000);
    };
    document.addEventListener('DOMContentLoaded', () => {
      const form = $('#schedule-form');
      resetSchedule();
      $('#new-schedule').addEventListener('click', resetSchedule);
      $('#refresh-schedules').addEventListener('click', refreshSchedules);
      $('#schedule-current').addEventListener('change', () => {
        if ($('#schedule-current').checked) {
          $('#schedule-video-dir').value = $('#video-dir').value;
          $('#schedule-music-dir').value = $('#music-dir').value;
          $('#schedule-output-dir').value = $('#output-dir').value;
        }
      });
      $$('.schedule-folder-picker').forEach(button => button.addEventListener('click', () => pickScheduleFolder(button.dataset.kind)));
      ['video', 'music', 'output'].forEach(kind => $(`#schedule-${kind}-dir`).addEventListener('input', () => { $('#schedule-current').checked = false; }));
      $('#schedule-check').addEventListener('click', async () => {
        try { renderSchedules(await api('/api/scheduler/check', { method: 'POST', body: '{}' })); }
        catch (error) { toast(error.message, true); }
      });
      form.addEventListener('submit', async event => {
        event.preventDefault();
        syncCurrentPaths();
        let savedConfig;
        try { savedConfig = form.dataset.editing === 'true' && !$('#schedule-current').checked ? JSON.parse(form.dataset.config || '{}') : configFromCurrentForm(); }
        catch (_) { savedConfig = configFromCurrentForm(); }
        const body = {
          id: form.dataset.scheduleId || undefined, name: form.elements.name.value.trim(), start_at: form.elements.start_at.value,
          repeat: form.elements.repeat.value, target_count: Number(form.elements.target_count.value),
          check_interval_minutes: Number(form.elements.check_interval_minutes.value), enabled: form.elements.enabled.checked,
          video_dir: $('#schedule-video-dir').value.trim(), music_dir: $('#schedule-music-dir').value.trim(), output_dir: $('#schedule-output-dir').value.trim(), config: savedConfig
        };
        try { renderSchedules(await api('/api/schedules', { method: 'POST', body: JSON.stringify(body) })); toast('定时任务已保存。'); resetSchedule(); }
        catch (error) { toast(error.message, true); }
      });
      window.addEventListener('hashchange', schedulerViewChanged);
      document.addEventListener('mixcut-bootstrap', schedulerViewChanged);
      schedulerViewChanged();
    });
  })();
})();

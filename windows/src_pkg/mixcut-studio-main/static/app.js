(() => {
  'use strict';
  const state = { scan: { videos: [], music: [], errors: [] }, batch: null, batches: [], poller: null, mediaCache: new Map(), keyframeObserver: null, batchSignature: '' };
  const $ = (s, root = document) => root.querySelector(s);
  const $$ = (s, root = document) => [...root.querySelectorAll(s)];
  const fmt = (seconds) => { const raw = Number(seconds); if (!Number.isFinite(raw)) return '未知时长'; const total = Math.max(0, Math.round(raw)), h = Math.floor(total / 3600), m = Math.floor(total % 3600 / 60), s = total % 60; return (h ? `${h}:` : '') + `${String(m).padStart(h ? 2 : 1, '0')}:${String(s).padStart(2, '0')}`; };
  const safeClass = (value) => String(value || 'pending').toLowerCase().replace(/[^a-z0-9_-]/g, '');
  const toast = (message, isError = false) => { const el = $('#toast'); el.textContent = message; el.style.background = isError ? '#9c4035' : ''; el.classList.add('show'); clearTimeout(toast.timer); toast.timer = setTimeout(() => el.classList.remove('show'), 4200); };
  const api = async (path, options = {}) => { let res; try { res = await fetch(path, { headers: { 'Content-Type': 'application/json', ...(options.headers || {}) }, ...options }); } catch (_) { throw new Error('无法连接本地服务。请确认启动器仍在运行，然后刷新页面。'); } let data = null; try { data = await res.json(); } catch (_) {} if (!res.ok) throw new Error((data && (data.error || data.message || data.detail)) || `请求失败（${res.status}）`); return data; };
  const setServer = (online, message) => { const el = $('#server-state'); el.className = `server-state ${online ? 'online' : 'error'}`; el.lastChild.textContent = ` ${message}`; };
  const currentView = () => location.hash.slice(1) || 'assets';
  const showView = () => { const view = currentView(); $$('.view').forEach(el => el.hidden = el.id !== view); $$('.nav a').forEach(el => el.classList.toggle('active', el.dataset.view === view)); if (view === 'tasks') refreshBatches(); else stopPolling(); };
  const stopPolling = () => { if (state.poller) clearInterval(state.poller); state.poller = null; };
  const setupPolling = () => { stopPolling(); state.poller = setInterval(() => { if (currentView() === 'tasks') refreshBatches(true); }, 3000); };
  const renderErrors = (errors = []) => { const box = $('#scan-errors'); box.replaceChildren(); if (!errors.length) { box.hidden = true; return; } errors.forEach(error => { const line = document.createElement('div'); line.textContent = typeof error === 'string' ? error : (error.message || JSON.stringify(error)); box.append(line); }); box.hidden = false; };
  const assetMeta = (asset) => [fmt(asset.duration), asset.width && asset.height ? `${asset.width}×${asset.height}` : '', asset.fps ? `${asset.fps} fps` : ''].filter(Boolean).join(' · ');
  const thumbnail = (asset) => `/api/thumbnail?id=${encodeURIComponent(asset.id)}`;
  const assetPrefs = () => { try { return JSON.parse(localStorage.getItem('mixcut.asset-prefs') || '{}'); } catch (_) { return {}; } };
  const saveAssetPrefs = () => { const prefs = assetPrefs(); $$('.video-card').forEach(card => { const id = $('.asset-select', card).dataset.id; prefs[id] = { selected: $('.asset-select', card).checked, group: $('.group-input', card).value }; }); $$('.music-card').forEach(card => { const id = $('.asset-select', card).dataset.id; prefs[id] = { selected: $('.asset-select', card).checked, first: $('.first-select', card).checked }; }); try { localStorage.setItem('mixcut.asset-prefs', JSON.stringify(prefs)); } catch (_) {} };
  const media = (asset) => `/api/media?id=${encodeURIComponent(asset.id)}`;
  const clearScannedLibrary = () => { state.scan = { videos: [], music: [], errors: [] }; renderAssets(); toast('素材目录已变更，请重新扫描后再生成方案。'); };
  const persistPreference = async (key, value) => { if (!value) return; try { await api('/api/preferences', { method: 'POST', body: JSON.stringify({ [key]: value }) }); } catch (error) { toast(`保存目录失败：${error.message}`, true); } };
  const syncReviewDir = (value) => { $('#review-dir').value = value; $('#task-review-dir').value = value; };
  const approveItem = async (batchId, itemId, button) => { const review_dir = $('#task-review-dir').value.trim() || $('#review-dir').value.trim(); if (!review_dir) { toast('请先选择审核通过文件夹。', true); return; } button.disabled = true; button.textContent = '正在归档'; try { await api('/api/approve', { method: 'POST', body: JSON.stringify({ batch_id: batchId, item_id: itemId, review_dir }) }); toast('已复制到审核通过文件夹。'); await refreshBatches(); } catch (error) { button.disabled = false; button.textContent = '通过审核'; const line = document.createElement('div'); line.className = 'error'; line.textContent = `归档失败：${error.message}`; button.parentElement?.append(line); toast(error.message, true); } };
  const pickFolder = async (kind) => { const input = kind === 'review' ? $('#review-dir') : $(`#${kind}-dir`); if (!input) return; const buttons = $$('.folder-picker'); buttons.forEach(button => button.disabled = true); try { const data = await api('/api/pick-folder', { method: 'POST', body: JSON.stringify({ kind, current_path: input.value.trim() }) }); if (data.cancelled || !data.path) return; const changed = input.value.trim() !== data.path; input.value = data.path; if (kind === 'review') syncReviewDir(data.path); if ((kind === 'video' || kind === 'music') && changed) clearScannedLibrary(); if (kind === 'output' || kind === 'review') toast('已保存目录。'); } catch (error) { toast(error.message, true); } finally { buttons.forEach(button => button.disabled = false); } };
  const frameTime = (value) => { const seconds = Math.max(0, Number(value) || 0), minutes = Math.floor(seconds / 60), remainder = seconds % 60; return `${String(minutes).padStart(2, '0')}:${remainder.toFixed(3).padStart(6, '0')}`; };
  const frameUrl = (batch, item, index, size, version) => `/api/keyframe?batch=${encodeURIComponent(batch)}&item=${encodeURIComponent(item)}&index=${encodeURIComponent(index)}&size=${size}&v=${encodeURIComponent(version || '')}`;
  const batchesSignature = (batches) => JSON.stringify((batches || []).map(batch => [batch.id, batch.updated_at, batch.status, batch.output_folder, ...(batch.items || []).map(item => [item.id, item.status, item.progress, item.result?.output_size, item.result?.output_mtime_ns, item.review?.status, item.review?.path, item.review?.error, item.review?.approved_at, item.error]) ]));
  const hideFrameOverlay = () => { const overlay = $('#keyframe-overlay'); if (overlay) overlay.remove(); };
  const showFrameOverlay = (event, frame) => { hideFrameOverlay(); const overlay = document.createElement('div'); overlay.id = 'keyframe-overlay'; overlay.className = 'keyframe-overlay'; const image = document.createElement('img'); image.alt = `关键帧大图 ${frame.label}`; image.src = frame.large; const caption = document.createElement('span'); caption.textContent = `关键帧 · ${frame.label}`; overlay.append(image, caption); document.body.append(overlay); const x = Math.min((event.clientX || frame.button.getBoundingClientRect().right) + 14, innerWidth - overlay.offsetWidth - 10), y = Math.min((event.clientY || frame.button.getBoundingClientRect().bottom) + 14, innerHeight - overlay.offsetHeight - 10); overlay.style.left = `${Math.max(10, x)}px`; overlay.style.top = `${Math.max(10, y)}px`; };
  const addFrameButtons = (gallery, frames, start) => { const grid = $('.keyframe-grid', gallery), batch = gallery.dataset.batch, item = gallery.dataset.item, version = gallery.dataset.version; frames.slice(start).forEach(frame => { const button = document.createElement('button'); button.type = 'button'; button.className = 'keyframe'; const label = frameTime(frame.time); button.setAttribute('aria-label', `跳转到关键帧 ${label}`); const image = document.createElement('img'); image.loading = 'lazy'; image.alt = `关键帧 ${label}`; image.src = frameUrl(batch, item, frame.index, 'thumb', version); const time = document.createElement('time'); time.textContent = label; button.append(image, time); const descriptor = { button, label, large: frameUrl(batch, item, frame.index, 'large', version) }; button.addEventListener('pointerenter', event => showFrameOverlay(event, descriptor)); button.addEventListener('focus', event => showFrameOverlay(event, descriptor)); button.addEventListener('pointerleave', hideFrameOverlay); button.addEventListener('blur', hideFrameOverlay); button.addEventListener('click', () => { const video = gallery.closest('.task-item')?.querySelector('video'); if (video) seekFrame(video, frame.time); }); grid.append(button); }); };
  const seekFrame = (video, value) => { if (!video) return; const target = Number(value) || 0; if (video.readyState >= 1) { const paused = video.paused; video.currentTime = target; if (paused) video.pause(); return; } video.dataset.pendingSeek = String(target); video.preload = 'auto'; if (!video.dataset.waitingMetadata) { video.dataset.waitingMetadata = 'true'; video.addEventListener('loadedmetadata', () => { const latest = Number(video.dataset.pendingSeek); delete video.dataset.waitingMetadata; delete video.dataset.pendingSeek; if (Number.isFinite(latest)) { const paused = video.paused; video.currentTime = latest; if (paused) video.pause(); } }, { once: true }); } video.load(); };
  const renderGalleryFrames = (gallery, frames, expanded) => { hideFrameOverlay(); const grid = $('.keyframe-grid', gallery); grid.replaceChildren(); const sample = expanded || frames.length <= 12 ? frames : Array.from({ length: 12 }, (_, index) => frames[Math.round(index * (frames.length - 1) / 11)]); $('h5', gallery).textContent = expanded ? `全部${frames.length}张关键帧（按时间排序）` : (frames.length > 12 ? `概览12张 · 共${frames.length}关键帧` : `关键帧 · 共 ${frames.length} 张（并非全部视频帧）`); addFrameButtons(gallery, sample, 0); $('.keyframe-more', gallery)?.remove(); if (frames.length > 12) { const toggle = document.createElement('button'); toggle.type = 'button'; toggle.className = 'button keyframe-more'; toggle.textContent = expanded ? '收起概览（12 张）' : `展开全部（${frames.length} 张）`; toggle.addEventListener('click', () => renderGalleryFrames(gallery, frames, !expanded)); gallery.append(toggle); } };
  const loadGallery = async (gallery) => { if (gallery.dataset.loaded) return; gallery.dataset.loaded = 'loading'; try { const data = await api(`/api/keyframes?batch=${encodeURIComponent(gallery.dataset.batch)}&item=${encodeURIComponent(gallery.dataset.item)}`); const frames = data.frames || []; gallery.dataset.loaded = 'true'; gallery.dataset.version = data.version || ''; renderGalleryFrames(gallery, frames, false); } catch (error) { gallery.dataset.loaded = ''; const heading = $('h5', gallery); heading.textContent = `关键帧暂不可用：${error.message}`; const retry = document.createElement('button'); retry.type = 'button'; retry.className = 'button keyframe-more'; retry.textContent = '重试加载关键帧'; retry.addEventListener('click', () => { retry.remove(); loadGallery(gallery); }); gallery.append(retry); } };
  const observeGallery = (gallery) => { if (!state.keyframeObserver) state.keyframeObserver = new IntersectionObserver(entries => entries.forEach(entry => { if (entry.isIntersecting) { state.keyframeObserver.unobserve(entry.target); loadGallery(entry.target); } }), { rootMargin: '180px 0px' }); state.keyframeObserver.observe(gallery); };
  document.addEventListener('keydown', event => { if (event.key === 'Escape') hideFrameOverlay(); });
  const captureCompletedMedia = () => $$('video[data-media-key]').forEach(video => { const gallery = video.parentElement?.querySelector('.keyframe-gallery'); if (gallery) state.mediaCache.set(video.dataset.mediaKey, { video, gallery, fingerprint: video.dataset.fingerprint || '' }); });
  const hydrateGalleries = () => { $$('video[src*="/api/output?"]').forEach(video => { const url = new URL(video.src, location.href), batch = url.searchParams.get('batch'), item = url.searchParams.get('item'); if (!batch || !item) return; const record = (state.batches.find(entry => String(entry.id) === batch)?.items || []).find(entry => String(entry.id) === item) || {}; const key = `${batch}:${item}`, fingerprint = `${record.result?.output_size || ''}:${record.result?.output_mtime_ns || ''}`; const cached = state.mediaCache.get(key); if (cached && cached.fingerprint === fingerprint) { video.replaceWith(cached.video); cached.gallery.remove(); cached.video.parentElement?.append(cached.gallery); return; } video.dataset.mediaKey = key; video.dataset.fingerprint = fingerprint; if (video.parentElement?.querySelector('.keyframe-gallery')) return; const gallery = document.createElement('section'); gallery.className = 'keyframe-gallery'; gallery.dataset.batch = batch; gallery.dataset.item = item; gallery.dataset.fingerprint = fingerprint; const heading = document.createElement('h5'); heading.textContent = '关键帧 · 即将按需加载'; const grid = document.createElement('div'); grid.className = 'keyframe-grid'; gallery.append(heading, grid); video.parentElement?.append(gallery); observeGallery(gallery); }); };
  function renderAssets() {
    const videos = state.scan.videos || [], music = state.scan.music || [];
    $('#video-count').textContent = `${videos.length} 条`; $('#music-count').textContent = `${music.length} 首`;
    $('#asset-summary').textContent = videos.length || music.length ? `已识别 ${videos.length} 个视频、${music.length} 首音乐。请在下一步选择参与规划的素材。` : '尚未找到可用素材。';
    const videoBox = $('#video-assets'), musicBox = $('#music-assets'); videoBox.replaceChildren(); musicBox.replaceChildren();
    if (!videos.length) videoBox.textContent = '没有可用视频。请检查目录、格式或扫描错误。';
    if (!music.length) musicBox.textContent = '没有可用音乐。请检查目录、格式或扫描错误。';
    const prefs = assetPrefs(); videos.forEach(asset => { const card = $('#video-card-template').content.firstElementChild.cloneNode(true); const image = $('img', card); image.src = thumbnail(asset); image.alt = `${asset.name || '视频'} 缩略图`; image.onerror = () => { image.removeAttribute('src'); image.alt = '缩略图不可用'; }; const check = $('.asset-select', card); check.checked = prefs[asset.id]?.selected ?? true; check.dataset.id = asset.id; $('.asset-name', card).textContent = asset.name || asset.path || '未命名视频'; $('.asset-meta', card).textContent = assetMeta(asset); const group = $('.group-input', card); group.value = prefs[asset.id]?.group ?? asset.group ?? ''; group.dataset.id = asset.id; videoBox.append(card); });
    music.forEach(asset => { const card = $('#music-card-template').content.firstElementChild.cloneNode(true); const check = $('.asset-select', card); check.checked = prefs[asset.id]?.selected ?? true; check.dataset.id = asset.id; $('.asset-name', card).textContent = asset.name || asset.path || '未命名音乐'; $('.asset-meta', card).textContent = [asset.music_style, fmt(asset.duration)].filter(Boolean).join(' · '); $('audio', card).src = media(asset); const first = $('.first-select', card); first.checked = prefs[asset.id]?.first ?? false; first.dataset.id = asset.id; musicBox.append(card); });
    renderPickers();
  }
  function renderPickers() {
    const videoBox = $('#video-picker'), musicBox = $('#music-picker'); videoBox.replaceChildren(); musicBox.replaceChildren();
    const selectedVideo = new Set($$('.video-card .asset-select:checked').map(el => el.dataset.id)); const selectedMusic = new Set($$('.music-card .asset-select:checked').map(el => el.dataset.id)); const firstMusic = new Set($$('.music-card .first-select:checked').map(el => el.dataset.id)); const groups = new Map($$('.group-input').map(el => [el.dataset.id, el.value]));
    (state.scan.videos || []).forEach(asset => { const row = document.createElement('label'); row.className = 'pick-row'; const input = document.createElement('input'); input.type = 'checkbox'; input.checked = selectedVideo.has(asset.id); input.dataset.id = asset.id; input.addEventListener('change', syncAssetChoices); const name = document.createElement('span'); name.textContent = asset.name || asset.path; const group = document.createElement('span'); group.className = 'duration'; group.textContent = groups.get(asset.id) ? `组：${groups.get(asset.id)}` : fmt(asset.duration); row.append(input, name, group); videoBox.append(row); });
    (state.scan.music || []).forEach(asset => { const row = document.createElement('label'); row.className = 'pick-row'; const input = document.createElement('input'); input.type = 'checkbox'; input.checked = selectedMusic.has(asset.id); input.dataset.id = asset.id; input.addEventListener('change', syncAssetChoices); const name = document.createElement('span'); name.textContent = asset.music_style ? `${asset.name || asset.path} · ${asset.music_style}` : (asset.name || asset.path); const duration = document.createElement('span'); duration.className = 'duration'; duration.textContent = fmt(asset.duration); const first = document.createElement('input'); first.type = 'checkbox'; first.checked = firstMusic.has(asset.id); first.dataset.id = asset.id; first.className = 'first-pick'; first.title = '可作为第一首'; first.addEventListener('change', syncAssetChoices); row.append(input, name, duration, first); musicBox.append(row); });
    videoBox.classList.toggle('empty', !state.scan.videos?.length); musicBox.classList.toggle('empty', !state.scan.music?.length); if (!state.scan.videos?.length) videoBox.textContent = '请先扫描视频。'; if (!state.scan.music?.length) musicBox.textContent = '请先扫描音乐。';
  }
  function syncAssetChoices() { const source = this; const selector = source.closest('#video-picker') ? '.video-card' : '.music-card'; const target = $(`${selector} .asset-select[data-id="${CSS.escape(source.dataset.id)}"]`); if (target && !source.classList.contains('first-pick')) target.checked = source.checked; if (source.classList.contains('first-pick')) { const original = $(`.music-card .first-select[data-id="${CSS.escape(source.dataset.id)}"]`); if (original) original.checked = source.checked; } saveAssetPrefs(); renderPickers(); }
  function getConfig() {
    const form = $('#config-form'), values = Object.fromEntries(new FormData(form)); const num = ['count','min_source_duration_minutes','min_duration','max_duration','min_songs','max_songs','start_gap','segment_min','segment_max','seed','fps','original_volume','music_volume']; num.forEach(key => values[key] = Number(values[key])); [values.width, values.height] = String(values.resolution || '1280x720').split('x').map(Number); delete values.resolution; values.allow_overlap = form.elements.allow_overlap.checked; values.within_group = values.within_group === 'true'; values.output_dir = $('#output-dir').value.trim(); values.video_ids = $$('.video-card .asset-select:checked').map(el => el.dataset.id); values.music_ids = $$('.music-card .asset-select:checked').map(el => el.dataset.id); values.first_song_ids = $$('.music-card .first-select:checked').map(el => el.dataset.id); values.groups = Object.fromEntries($$('.group-input').map(el => [el.dataset.id, el.value.trim()]).filter(([, value]) => value));
    values.sticker_template_id = values.sticker_template_id || null; if (!values.video_ids.length) throw new Error('请至少选择一个视频素材。'); if (!values.music_ids.length) throw new Error('请至少选择一首音乐。'); if (!values.output_dir) throw new Error('请在素材页填写导出文件夹。'); if (values.min_duration > values.max_duration) throw new Error('最短时长不能大于最长时长。'); if (values.segment_min > values.segment_max) throw new Error('单段最短时长不能大于最长时长。'); return values;
  }
  function stat(label, value) { const box = document.createElement('div'); box.className = 'stat'; const strong = document.createElement('b'); strong.textContent = value; const span = document.createElement('span'); span.textContent = label; box.append(strong, span); return box; }
  const appendList = (root, title, values, map) => { const track = document.createElement('div'); track.className = 'track'; const h = document.createElement('h4'); h.textContent = title; const list = document.createElement('ol'); values.forEach(value => { const li = document.createElement('li'); li.textContent = map(value); list.append(li); }); track.append(h, list); root.append(track); };
  function renderPlan(batch) {
    state.batch = batch; $('#plan-message').textContent = batch ? `批次 ${batch.folder_name || batch.id} 已生成。仅在核对后开始渲染。${batch.output_folder ? ` 输出目录：${batch.output_folder}` : ''}` : '尚未生成方案。'; $('#start-btn').disabled = !batch || !batch.items?.length || batch.status !== 'draft'; $('#manifest-btn').disabled = !batch; const stats = $('#plan-stats'), warnings = $('#plan-warnings'), list = $('#plan-items'); stats.replaceChildren(); warnings.replaceChildren(); list.replaceChildren(); if (!batch) return;
    if(batch.config?.sticker_layers?.length){const note=document.createElement('div');note.textContent=`贴图模板：${batch.config.sticker_template?.name || '已保存模板'} · ${batch.config.sticker_layers.length}层（位置、大小和出现时间已固定到本批次）`;warnings.append(note);}
    const s = batch.stats || {}; const statLabels = { max_overlap_ratio: '最高两两重合', source_usage: '素材使用', video_usage: '视频素材使用', music_usage: '音乐素材使用', unique_music_orders: '不同歌曲顺序', unique_video_plans: '不同视频方案', estimated_output_bytes: '预计输出大小', total_duration: '总成片时长' }; const names = new Map([...state.scan.videos || [], ...state.scan.music || []].map(asset => [asset.id, asset.name || asset.path || asset.id])); const usage = value => Object.entries(value || {}).slice(0, 3).map(([id, count]) => `${names.get(id) || String(id).slice(0, 10)} ×${count}`).join('；') || '—'; const formatted = (key, value) => key === 'max_overlap_ratio' && Number.isFinite(Number(value)) ? `${Math.round(Number(value) * 100)}%` : key === 'estimated_output_bytes' && Number.isFinite(Number(value)) ? `${(Number(value) / 1024 ** 3).toFixed(2)} GB` : key === 'total_duration' && Number.isFinite(Number(value)) ? fmt(value) : (key.includes('usage') && typeof value === 'object' ? usage(value) : (typeof value === 'object' ? JSON.stringify(value) : String(value))); stats.append(stat('计划条数', batch.items?.length ?? 0), stat('目标数量', batch.config?.count ?? '—')); Object.entries(s).filter(([key]) => !['duplicate_count', 'count'].includes(key)).forEach(([key, value]) => stats.append(stat(statLabels[key] || key.replaceAll('_', ' '), formatted(key, value))));
    (batch.warnings || []).forEach(w => { const line = document.createElement('div'); line.textContent = typeof w === 'string' ? w : (w.message || JSON.stringify(w)); warnings.append(line); });
    (batch.items || []).forEach(item => { const card = document.createElement('article'); card.className = 'plan-item'; const head = document.createElement('div'); head.className = 'item-head'; const title = document.createElement('h3'); title.textContent = item.output_name || `成片 ${item.index ?? '—'}`; const duration = document.createElement('span'); duration.className = 'duration'; duration.textContent = fmt(item.duration); head.append(title, duration); const tracks = document.createElement('div'); tracks.className = 'tracks'; appendList(tracks, '视频区间', item.segments || [], segment => `${segment.path || segment.asset_id} · ${fmt(segment.start)} 起，${fmt(segment.duration)}`); appendList(tracks, '完整歌曲顺序', item.music || [], song => song.name || song.path || song.asset_id || String(song)); card.append(head, tracks); list.append(card); });
  }
  async function scan() { const video_dir = $('#video-dir').value.trim(), music_dir = $('#music-dir').value.trim(); if (!video_dir || !music_dir) return toast('请先填写视频和音乐文件夹。', true); const btn = $('#scan-btn'); btn.disabled = true; btn.textContent = '正在扫描…'; renderErrors([]); try { const data = await api('/api/scan', { method: 'POST', body: JSON.stringify({ video_dir, music_dir }) }); state.scan = data.scan || data; renderAssets(); renderErrors(state.scan.errors || []); toast(`扫描完成：${state.scan.videos?.length || 0} 个视频，${state.scan.music?.length || 0} 首音乐。`); } catch (error) { renderErrors([error.message]); toast(error.message, true); } finally { btn.disabled = false; btn.textContent = '扫描素材'; } }
  async function createPlan() { let config; try { config = getConfig(); } catch (error) { toast(error.message, true); return; } const btn = $('#plan-btn'); btn.disabled = true; btn.textContent = '正在规划…'; try { const data = await api('/api/plan', { method: 'POST', body: JSON.stringify({ config }) }); renderPlan(data.batch || data); location.hash = 'plan'; toast('方案生成完成，请核对每条歌曲与视频区间。'); } catch (error) { renderPlan(null); $('#plan-message').textContent = `无法生成方案：${error.message}`; location.hash = 'plan'; toast(error.message, true); } finally { btn.disabled = false; btn.textContent = '生成方案'; } }
  async function batchAction(id, action) { try { const data = await api(`/api/batches/${encodeURIComponent(id)}/${action}`, { method: 'POST', body: '{}' }); if (data.batch && state.batch?.id === id) renderPlan(data.batch); toast({start:'已加入渲染队列',pause:'将在当前成片完成后暂停',resume:'已继续队列',stop:'已停止队列',retry:'已安排重试'}[action] || '操作成功'); if (action === 'start') location.hash = 'tasks'; refreshBatches(); } catch (error) { toast(error.message, true); } }
  function renderBatches(batches) { const box = $('#batches'); box.replaceChildren(); if (!batches.length) { box.className = 'batch-list empty'; box.textContent = '尚无任务。先生成方案并确认开始渲染。'; $('#task-summary').replaceChildren(stat('等待批次', '—')); return; } box.className = 'batch-list'; const items = batches.flatMap(b => b.items || []); const counts = items.reduce((out, item) => { const k = safeClass(item.status); out[k] = (out[k] || 0) + 1; return out; }, {}); const summary = $('#task-summary'); summary.replaceChildren(stat('总条数', items.length), stat('完成', counts.completed || counts.success || 0), stat('处理中', (counts.running || 0) + (counts.processing || 0) + (counts.validating || 0)), stat('失败', counts.failed || counts.error || 0), stat('待处理', counts.pending || 0), stat('已取消', counts.cancelled || 0));
    const statusLabels = { draft:'草稿', queued:'排队中', running:'渲染中', pausing:'等待当前条完成', paused:'已暂停', stopping:'正在停止', stopped:'已停止', completed:'已完成', failed:'失败', pending:'待处理', success:'已完成', processing:'处理中', error:'失败', cancelled:'已取消' }; batches.forEach(batch => { const card = document.createElement('article'); card.className = 'batch'; const batchStatus = safeClass(batch.status); const head = document.createElement('div'); head.className = 'batch-head'; const title = document.createElement('h3'); title.textContent = `批次 ${batch.id}`; const status = document.createElement('span'); status.className = `status ${batchStatus}`; status.textContent = statusLabels[batchStatus] || batch.status || '未知状态'; head.append(title, status); if(batch.schedule_name){const tag=document.createElement('small');tag.textContent=`定时任务：${batch.schedule_name}`;head.append(tag);} const all = batch.items?.length || 0, done = (batch.items || []).filter(i => ['completed','success'].includes(String(i.status).toLowerCase())).length, failed = (batch.items || []).some(i => ['failed','error'].includes(safeClass(i.status))); const progress = document.createElement('div'); progress.className = 'progress'; const bar = document.createElement('i'); bar.style.width = `${all ? Math.round(done / all * 100) : 0}%`; progress.append(bar); const controls = document.createElement('div'); controls.className = 'batch-controls'; const allowed = { pause:['queued','running'], resume:['paused'], stop:['queued','running','pausing','paused'], retry:['failed','stopped','completed'] }; [['pause','暂停（当前条完成后）'],['resume','继续'],['stop','停止'],['retry','重试失败项']].forEach(([action, label]) => { const button = document.createElement('button'); button.className = 'button'; button.textContent = label; button.disabled = !allowed[action].includes(batchStatus) || (action === 'retry' && !failed); button.onclick = () => batchAction(batch.id, action); controls.append(button); }); const view = document.createElement('button'); view.className = 'button'; view.textContent = '查看方案'; view.onclick = () => { renderPlan(batch); location.hash = 'plan'; }; controls.append(view); const reveal = document.createElement('button'); reveal.className = 'button'; reveal.textContent = '在 Finder 中显示'; reveal.onclick = async () => { try { await api('/api/reveal', { method:'POST', body:JSON.stringify({ batch_id: batch.id }) }); } catch (e) { toast(e.message, true); } }; controls.append(reveal); if (batch.error || batch.recovery_note) { const note = document.createElement('div'); note.className = 'notice'; note.textContent = batch.error || batch.recovery_note; card.append(note); } const taskItems = document.createElement('div'); taskItems.className = 'task-items'; (batch.items || []).forEach(item => { const row = document.createElement('div'); row.className = 'task-item'; const index = document.createElement('span'); index.textContent = String(item.index ?? '—').padStart(2, '0'); const label = document.createElement('span'); const pct = Number.isFinite(Number(item.progress)) ? ` · ${Math.round(Number(item.progress) * 100)}%` : ''; label.textContent = `${item.output_name ? item.output_name + ' · ' : ''}${fmt(item.duration)}${pct}`; const itemStatus = document.createElement('span'); const itemState = safeClass(item.status); itemStatus.className = `status ${itemState}`; itemStatus.textContent = statusLabels[itemState] || item.status || '待处理'; row.append(index,label,itemStatus); if (item.error) { const error = document.createElement('div'); error.className = 'error'; error.textContent = item.error; row.append(error); } if (['completed','success'].includes(String(item.status).toLowerCase())) { const video = document.createElement('video'); video.controls = true; video.preload = 'none'; video.src = `/api/output?batch=${encodeURIComponent(batch.id)}&item=${encodeURIComponent(item.id)}`; row.append(video); } taskItems.append(row); }); card.append(head, progress, controls, taskItems); box.append(card); });
  }
  async function refreshBatches(silent = false) { if (silent && $$('video').some(video => !video.paused && !video.ended)) return; try { const data = await api('/api/batches'); const batches = data.batches || data || []; const signature = batchesSignature(batches); if (signature === state.batchSignature) { if (currentView() === 'tasks' && !state.poller) setupPolling(); return; } captureCompletedMedia(); state.batches = batches; state.batchSignature = signature; renderBatches(state.batches); hydrateGalleries(); setupPolling(); } catch (error) { if (!silent) toast(error.message, true); setServer(false, '本地服务未连接'); } }
  async function bootstrap() { try { const data = await api('/api/bootstrap'); $('#video-dir').value = data.video_dir || ''; $('#music-dir').value = data.music_dir || ''; $('#output-dir').value = data.output_dir || ''; syncReviewDir(data.review_dir || ''); state.scan = data.scan || state.scan; state.batches = data.batches || []; state.batchSignature = batchesSignature(state.batches); renderAssets(); updateStickerSelectors(); renderErrors(state.scan.errors || []); renderBatches(state.batches); hydrateGalleries(); const draft = state.batches.find(batch => batch.status === 'draft'); if (draft) renderPlan(draft); setServer(true, data.ffmpeg_available === false ? '服务已连接 · 未找到 FFmpeg' : '本地服务已连接'); document.dispatchEvent(new Event('mixcut-bootstrap')); } catch (error) { setServer(false, '本地服务未连接'); renderErrors([error.message]); } }
  document.addEventListener('DOMContentLoaded', () => { window.addEventListener('hashchange', showView); showView(); $('#scan-btn').addEventListener('click', scan); $('#plan-btn').addEventListener('click', createPlan); $('#start-btn').addEventListener('click', () => state.batch && batchAction(state.batch.id, 'start')); $('#manifest-btn').addEventListener('click', () => { if (state.batch) window.open(`/api/manifest?batch=${encodeURIComponent(state.batch.id)}`, '_blank', 'noopener'); }); $('#refresh-tasks').addEventListener('click', () => refreshBatches()); $('#shutdown-btn').addEventListener('click', async () => { if (!confirm('确定停止本地后台服务吗？正在处理的任务会停止。')) return; try { await api('/api/shutdown', { method:'POST', body:'{}' }); } catch (error) { toast(error.message, true); return; } stopPolling(); setServer(false, '本地服务已停止'); toast('已请求停止后台服务。'); }); $('#config-form').addEventListener('change', event => { if (event.target.name === 'mode') { $$('.mode-multi').forEach(el => el.hidden = event.target.value !== 'multi'); $$('.mode-single').forEach(el => el.hidden = event.target.value !== 'single'); } if (event.target.name === 'music_mode') $$('.pool-settings').forEach(el => el.hidden = event.target.value !== 'pool'); }); $('#video-assets').addEventListener('change', event => { if (event.target.matches('.asset-select,.group-input')) { saveAssetPrefs(); renderPickers(); } }); $('#music-assets').addEventListener('change', event => { if (event.target.matches('.asset-select,.first-select')) { saveAssetPrefs(); renderPickers(); } }); bootstrap(); });
  document.addEventListener('DOMContentLoaded', () => { $$('.folder-picker').forEach(button => button.addEventListener('click', () => pickFolder(button.dataset.kind))); $('#output-dir').addEventListener('change', () => persistPreference('output_dir', $('#output-dir').value.trim())); ['review-dir', 'task-review-dir'].forEach(id => $(`#${id}`).addEventListener('change', () => { const value = $(`#${id}`).value.trim(); syncReviewDir(value); persistPreference('review_dir', value); })); ['video-dir', 'music-dir'].forEach(id => $(`#${id}`).addEventListener('change', () => { if (state.scan.videos.length || state.scan.music.length) clearScannedLibrary(); })); });
  const polishTaskControls = () => $$('.batch').forEach(card => { const badge = $('.batch-head .status', card); if (badge?.textContent === 'validating') badge.textContent = '校验中'; const batchId = $('.batch-head h3', card)?.textContent.replace('批次 ', ''); const batch = state.batches.find(item => String(item.id) === batchId); if (batch?.output_folder && !$('.output-location', card)) { const output = document.createElement('p'); output.className = 'output-location field-help'; output.textContent = `输出目录：${batch.output_folder}`; $('.batch-head', card).after(output); } const hasFailed = $$('.task-item .status', card).some(el => ['failed', 'error', '失败'].includes(safeClass(el.textContent)) || el.textContent === '失败'); const hasPending = $$('.task-item .status', card).some(el => el.textContent === '待处理'); const stateText = badge?.textContent; $$('.batch-controls .button', card).forEach(button => { if (button.textContent === '重试失败项' && hasFailed && stateText === '已暂停') button.disabled = false; if (button.textContent === '继续' && hasPending && stateText === '已停止') button.disabled = false; }); });
  new MutationObserver(polishTaskControls).observe($('#batches'), { childList: true, subtree: true });
  const polishOutputFolders = () => $$('.batch').forEach(card => { const batchId = $('.batch-head h3', card)?.textContent.replace('批次 ', ''); const batch = state.batches.find(item => String(item.id) === batchId); const output = $('.output-location', card); const text = batch && `批次 ${batch.folder_name || batch.id} · 输出目录：${batch.output_folder}`; if (output && text && output.textContent !== text) output.textContent = text; });
  new MutationObserver(polishOutputFolders).observe($('#batches'), { childList: true, subtree: true });
  const polishReviewActions = () => $$('.batch').forEach(card => {
    const batchId = $('.batch-head h3', card)?.textContent.replace('批次 ', '');
    const batch = state.batches.find(entry => String(entry.id) === batchId);
    if (!batch) return;
    $$('.task-item', card).forEach(row => {
      if ($('.review-action', row)) return;
      const video = $('video', row);
      if (!video) return;
      const itemId = new URL(video.src, location.href).searchParams.get('item');
      const item = (batch.items || []).find(entry => String(entry.id) === itemId);
      if (!item) return;
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
      } else {
        const approve = document.createElement('button');
        approve.className = 'button primary'; approve.type = 'button';
        approve.disabled = item.review?.status === 'copying';
        approve.textContent = approve.disabled ? '正在归档' : '通过审核';
        approve.addEventListener('click', () => approveItem(batch.id,item.id,approve));
        action.append(approve);
        const variant = document.createElement('select'); variant.className = 'sticker-variant'; variant.setAttribute('aria-label','审核时追加的贴图模板'); variant.append(new Option('不追加贴图', '')); (state.stickers?.templates || []).forEach(template => variant.append(new Option(template.name, template.id))); const makeVariant = document.createElement('button'); makeVariant.className = 'button'; makeVariant.type = 'button'; makeVariant.textContent = '生成贴图版本'; makeVariant.addEventListener('click', async () => { if (!variant.value) return toast('请选择一个贴图模板。', true); makeVariant.disabled = true; try { const job = await api('/api/sticker-variants', { method:'POST', body:JSON.stringify({batch_id:batch.id,item_id:item.id,template_id:variant.value}) }); makeVariant.textContent = '贴图版本排队中'; const timer = setInterval(async () => { try { const status = await api(`/api/sticker-variants/${encodeURIComponent(job.job_id)}`); makeVariant.textContent = status.status === 'completed' ? '贴图版本已生成' : `生成中 ${Math.round((status.progress || 0) * 100)}%`; if (['completed','failed'].includes(status.status)) { clearInterval(timer); makeVariant.disabled = false; if (status.status === 'failed') toast(status.error || '贴图版本生成失败', true); await refreshBatches(); } } catch (_) { clearInterval(timer); makeVariant.disabled = false; } }, 3000); } catch (error) { makeVariant.disabled = false; toast(error.message,true); } }); const hint=document.createElement('p');hint.className='field-help';hint.textContent='选模板后请生成并审核新版本；原视频保持不变。';makeVariant.disabled=!variant.value;variant.addEventListener('change',()=>{makeVariant.disabled=!variant.value;approve.disabled=!!variant.value || item.review?.status==='copying';approve.textContent=variant.value?'请先生成贴图版本':(approve.disabled?'正在归档':'通过审核');});action.append(hint, variant, makeVariant);
        if (item.review?.error) {
          const error = document.createElement('div'); error.className = 'review-error';
          error.textContent = `归档失败：${item.review.error}`; action.append(error);
        }
      }
      video.before(action);
    });
  });
  new MutationObserver(polishReviewActions).observe($('#batches'), { childList: true, subtree: true });
  state.stickers = { assets: [], templates: [], active: null, layers: [] };
  const stickerImage = id => `/api/sticker?id=${encodeURIComponent(id)}`;
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
      config.allow_overlap = form.elements.allow_overlap.checked;
      config.within_group = config.within_group === 'true';
      config.sticker_template_id = config.sticker_template_id || null;
      config.first_song_ids = $$('.music-card .first-select:checked').map(input => input.dataset.id);
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

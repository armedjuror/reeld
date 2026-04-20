// ─── Media URLs ───────────────────────────────────────────────────────────────
function mediaUrl(path) {
  if (!path) return '';
  return '/api/media/' + path;
}

// ─── State ────────────────────────────────────────────────────────────────────
const state = {
  presets: [],
  editingPreset: null,
  tempPresetFiles: {},
  currentUser: null,

  // current reel being edited
  reelId: null,
  reelName: null,
  selectedPresetId: null,
  audioPath: null,
  rawAudioPath: null,    // original uploaded audio (for re-running silence removal)
  silenceReady: false,   // clean step done or skipped
  introPath: null,
  outroPath: null,
  segments: [],
  step: 1,

  // audio preview
  audioPlayer: null,
  bgmPlayer: null,
  bgmFadeTimer: null,
  segTrackInterval: null,
  playingIndex: null,
  mainPlaying: false,
  mainPaused: false,
  mainPausedIndex: null,

  // drag-to-reorder
  dragSrcIndex: null,

  // undo/redo
  segmentHistory: [],
  segmentFuture: [],

  // canvas drag state
  canvasDrag: {}   // { vis|frame|text: {dragging, startX, startY} }
};

// ─── Init ─────────────────────────────────────────────────────────────────────
window.addEventListener('load', async () => {
  // Check auth — show login overlay if not signed in
  try {
    const r = await fetch('/auth/me');
    if (!r.ok) { showLoginOverlay(); return; }
    const user = await r.json();
    state.currentUser = user;
    showUserInSidebar(user);
    // Handle /import/<token> route
    const match = window.location.pathname.match(/^\/import\/([^/]+)$/);
    if (match) { await handleImportRoute(match[1]); return; }
  } catch { showLoginOverlay(); return; }

  await loadPresets();
  renderPresetsGrid();
  populatePresetSelect();
  await loadReels();
  initCanvases();
  loadCreditBalance();
});

function showLoginOverlay() {
  window.location.href = '/';
}

function showUserInSidebar(user) {
  const el = document.getElementById('sidebar-user');
  el.style.display = 'flex';
  document.getElementById('sidebar-avatar').src = user.avatar_url || '';
  document.getElementById('sidebar-name').textContent = user.name || user.email;
  document.getElementById('sidebar-email').textContent = user.email;
}

async function handleImportRoute(token) {
  try {
    const r = await fetch('/api/presets/import', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token })
    });
    if (!r.ok) { toast('Share link not found', 'error'); }
    else {
      const p = await r.json();
      toast(`Imported "${p.name}"`, 'success');
    }
  } catch { toast('Import failed', 'error'); }
  // Navigate to presets page
  await loadPresets(); renderPresetsGrid(); populatePresetSelect();
  await loadReels(); initCanvases();
  history.replaceState(null, '', '/');
  document.querySelectorAll('.nav-item').forEach(n => n.classList.toggle('active', n.dataset.page === 'presets'));
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.getElementById('page-presets').classList.add('active');
}

// ─── Undo / Redo ──────────────────────────────────────────────────────────────

function pushHistory() {
  state.segmentHistory.push(JSON.parse(JSON.stringify(state.segments)));
  state.segmentFuture = [];
  if (state.segmentHistory.length > 50) state.segmentHistory.shift();
}

function undoSegments() {
  if (!state.segmentHistory.length) { toast('Nothing to undo'); return; }
  state.segmentFuture.push(JSON.parse(JSON.stringify(state.segments)));
  state.segments = state.segmentHistory.pop();
  renderSegments();
  toast('Undo');
}

function redoSegments() {
  if (!state.segmentFuture.length) { toast('Nothing to redo'); return; }
  state.segmentHistory.push(JSON.parse(JSON.stringify(state.segments)));
  state.segments = state.segmentFuture.pop();
  renderSegments();
  toast('Redo');
}

document.addEventListener('keydown', e => {
  const active = document.activeElement;
  const inInput = active && (active.contentEditable === 'true' || active.tagName === 'INPUT' || active.tagName === 'TEXTAREA' || active.tagName === 'SELECT');

  if (e.key === ' ' && !e.ctrlKey && !e.metaKey && !e.altKey && !inInput) {
    const btn = document.getElementById('main-play-btn');
    if (btn && btn.style.display !== 'none') { e.preventDefault(); toggleMainPlay(); }
    return;
  }

  if ((e.ctrlKey || e.metaKey) && e.key === 's') { e.preventDefault(); saveReelDraft(); return; }

  if (!(e.ctrlKey || e.metaKey) || e.key !== 'z' && e.key !== 'Z' && e.key !== 'y' && e.key !== 'Y') return;
  if (inInput) return;
  e.preventDefault();
  if (e.key === 'y' || e.key === 'Y' || e.shiftKey) redoSegments();
  else undoSegments();
});

// ─── Navigation ───────────────────────────────────────────────────────────────
function showPage(name, el) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  const pageEl = document.getElementById(`page-${name}`);
  if (pageEl) pageEl.classList.add('active');
  if (el) el.classList.add('active');
}

// ─── Toast ────────────────────────────────────────────────────────────────────
function toast(msg, type = '') {
  const t = document.createElement('div');
  t.className = `toast-item ${type}`;
  t.textContent = msg;
  document.getElementById('toast').appendChild(t);
  setTimeout(() => t.remove(), 3500);
}

// ─── Helpers ──────────────────────────────────────────────────────────────────
function getVal(id) { const e = document.getElementById(id); return e ? e.value : ''; }
function setVal(id, val) { const e = document.getElementById(id); if (e) e.value = val; }
function fmtTime(s) { const m = Math.floor(s/60); return `${m}:${(s%60).toFixed(1).padStart(4,'0')}`; }
function fmtDuration(sec) { const m = Math.floor(sec/60); return `${m}:${String(Math.round(sec%60)).padStart(2,'0')}`; }
function updateRangeVal(rid, vid, fmt) { const r = document.getElementById(rid), v = document.getElementById(vid); if(r&&v) v.textContent = fmt(parseFloat(r.value)); }
function parsePos(val) { if (val==='center') return 'center'; const n = parseFloat(val); return isNaN(n) ? val : n; }

// ─── Presets ──────────────────────────────────────────────────────────────────
async function loadPresets() {
  try { const r = await fetch('/api/presets'); state.presets = await r.json(); }
  catch { state.presets = []; }
}

function renderPresetsGrid() {
  const grid = document.getElementById('presets-grid');
  const newCard = grid.querySelector('.new-preset-card');
  grid.innerHTML = '';
  grid.appendChild(newCard);
  state.presets.forEach(p => {
    const card = document.createElement('div');
    card.className = 'preset-card';
    card.innerHTML = `
      <div class="preset-card-preview">
        <div class="preview-phone">
          <div style="position:absolute;inset:0;background:${p.background?.color||'#0a0a0a'}"></div>
          <div style="padding:8px 6px;position:relative;z-index:1">
            <div class="preview-line" style="width:80%"></div>
            <div class="preview-line" style="width:60%"></div>
            <div class="preview-vis-block"></div>
            <div class="preview-line" style="width:40%;margin-top:6px;background:var(--accent);opacity:0.8"></div>
          </div>
        </div>
      </div>
      <div class="preset-card-info">
        <div class="preset-card-name">${p.name}</div>
        <div class="preset-card-meta">
          <span>${p.resolution?.w||1080}×${p.resolution?.h||1920}</span>
          <span>${p.bgm?.file ? '♪ BGM' : 'No BGM'}</span>
        </div>
        <div class="preset-card-actions">
          <button class="btn btn-secondary btn-sm" onclick="openPresetEditor('${p.id}')">Edit</button>
          <button class="btn btn-ghost btn-sm" onclick="sharePreset('${p.id}')" title="Copy share link">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/><line x1="8.59" y1="13.51" x2="15.42" y2="17.49"/><line x1="15.41" y1="6.51" x2="8.59" y2="10.49"/></svg>
            Share
          </button>
          <button class="btn btn-danger btn-sm" onclick="confirmDeletePreset('${p.id}')">Delete</button>
        </div>
      </div>`;
    grid.appendChild(card);
  });
}

function populatePresetSelect() {
  const sel = document.getElementById('preset-select');
  const cur = sel.value;
  sel.innerHTML = '<option value="">— Select a preset —</option>';
  state.presets.forEach(p => {
    const o = document.createElement('option');
    o.value = p.id; o.textContent = p.name;
    sel.appendChild(o);
  });
  if (state.selectedPresetId) sel.value = state.selectedPresetId;
  else if (cur) sel.value = cur;
}

// ─── Preset editor ────────────────────────────────────────────────────────────
function openPresetEditor(presetId) {
  state.tempPresetFiles = {};
  const editor = document.getElementById('preset-editor');
  if (presetId) {
    const p = state.presets.find(x => x.id === presetId);
    state.editingPreset = JSON.parse(JSON.stringify(p));
    document.getElementById('editor-title').textContent = 'Edit Preset';
    fillEditorForm(p);
  } else {
    state.editingPreset = null;
    document.getElementById('editor-title').textContent = 'New Preset';
    resetEditorForm();
  }
  editor.classList.add('open');
  document.querySelectorAll('.editor-tab').forEach((t,i) => t.classList.toggle('active', i===0));
  document.querySelectorAll('.editor-tab-pane').forEach((p,i) => p.classList.toggle('active', i===0));
  setTimeout(() => { initCanvases(); syncCanvasFromFields('vis'); syncCanvasFromFields('frame'); syncCanvasFromFields('text'); }, 100);
}

function closePresetEditor() {
  document.getElementById('preset-editor').classList.remove('open');
  state.editingPreset = null;
}

function switchTab(el, tabId) {
  document.querySelectorAll('.editor-tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.editor-tab-pane').forEach(p => p.classList.remove('active'));
  el.classList.add('active');
  document.getElementById(tabId).classList.add('active');
  if (tabId === 'tab-layout') setTimeout(() => { syncCanvasFromFields('vis'); syncCanvasFromFields('frame'); syncCanvasFromFields('text'); }, 50);
}

function fillEditorForm(p) {
  setVal('p-name', p.name);
  setVal('p-res-w', p.resolution?.w||1080);
  setVal('p-res-h', p.resolution?.h||1920);
  setVal('p-bg-type', p.background?.type||'color');
  setVal('p-bg-color', p.background?.color||'#0a0a0a');
  onBgTypeChange();
  if (p.background?.file) showFileSet('p-bg-file-name', p.background.file.split('/').pop());
  setVal('p-bgm-vol', p.bgm?.bgm_volume||0.15);
  setVal('p-voice-vol', p.bgm?.voice_volume||1);
  setVal('p-bgm-fade', p.bgm?.fade_out||2);
  updateRangeVal('p-bgm-vol','p-bgm-vol-val',v=>Math.round(v*100)+'%');
  updateRangeVal('p-voice-vol','p-voice-vol-val',v=>Math.round(v*100)+'%');
  if (p.bgm?.file) showFileSet('p-bgm-file-name', p.bgm.file.split('/').pop());
  if (p.caption?.font) showFileSet('p-font-file-name', p.caption.font.split('/').pop());
  setVal('p-text-color', p.caption?.text_color||'#ffffff');
  setVal('p-hi-mode', p.caption?.highlight?.mode||'text');
  setVal('p-hi-color', p.caption?.highlight?.color||'#f5a623');
  setVal('p-nv-size', p.caption?.without_visual?.font_size||72);
  setVal('p-nv-anim', p.caption?.without_visual?.animation||'typing');
  setVal('p-nv-speed', p.caption?.without_visual?.animation_speed||20);
  setVal('p-wv-size', p.caption?.with_visual?.font_size||52);
  setVal('p-wv-anim', p.caption?.with_visual?.animation||'fade');
  setVal('p-wv-speed', p.caption?.with_visual?.animation_speed||20);
  setVal('p-vis-x', p.visual?.container?.x||60);
  setVal('p-vis-y', p.visual?.container?.y||320);
  setVal('p-vis-w', p.visual?.container?.w||960);
  setVal('p-vis-h', p.visual?.container?.h||820);
  setVal('p-vis-anim-dur', p.visual?.animation_duration||0.5);
  const fe = p.frame?.enabled||false;
  document.getElementById('p-frame-enabled').checked = fe;
  document.getElementById('frame-config').style.display = fe ? 'block' : 'none';
  if (p.frame?.file) showFileSet('p-frame-file-name', p.frame.file.split('/').pop());
  setVal('p-frame-x', p.frame?.container?.x||40);
  setVal('p-frame-y', p.frame?.container?.y||300);
  setVal('p-frame-w', p.frame?.container?.w||1000);
  setVal('p-frame-h', p.frame?.container?.h||860);
  const wvPos = p.caption?.with_visual?.position||{};
  setVal('p-wv-x', wvPos.x==='center' ? 540 : (wvPos.x||540));
  setVal('p-wv-y', wvPos.y==='center' ? 960 : (wvPos.y||1500));
  setVal('p-trans-type', p.transitions?.type||'fade');
  setVal('p-trans-dur', p.transitions?.duration||0.3);
  document.getElementById('canvas-res-label').textContent = `${p.resolution?.w||1080}×${p.resolution?.h||1920}`;
}

function resetEditorForm() {
  ['p-name'].forEach(id => setVal(id, ''));
  setVal('p-res-w',1080); setVal('p-res-h',1920);
  setVal('p-bg-type','color'); setVal('p-bg-color','#0a0a0a');
  setVal('p-bgm-vol',0.15); setVal('p-voice-vol',1); setVal('p-bgm-fade',2);
  updateRangeVal('p-bgm-vol','p-bgm-vol-val',v=>Math.round(v*100)+'%');
  updateRangeVal('p-voice-vol','p-voice-vol-val',v=>Math.round(v*100)+'%');
  setVal('p-text-color','#ffffff'); setVal('p-hi-mode','text'); setVal('p-hi-color','#f5a623');
  setVal('p-nv-size',72); setVal('p-nv-anim','typing'); setVal('p-nv-speed',20);
  setVal('p-wv-size',52); setVal('p-wv-anim','fade'); setVal('p-wv-speed',20);
  setVal('p-vis-x',60); setVal('p-vis-y',320); setVal('p-vis-w',960); setVal('p-vis-h',820);
  setVal('p-vis-anim-dur',0.5);
  document.getElementById('p-frame-enabled').checked = false;
  document.getElementById('frame-config').style.display = 'none';
  setVal('p-frame-x',40); setVal('p-frame-y',300); setVal('p-frame-w',1000); setVal('p-frame-h',860);
  setVal('p-wv-x',540); setVal('p-wv-y',1500);
  setVal('p-trans-type','fade'); setVal('p-trans-dur',0.3);
  onBgTypeChange();
  ['p-bg-file-name','p-bgm-file-name','p-font-file-name','p-frame-file-name'].forEach(id => {
    const el = document.getElementById(id); if(el) el.style.display='none';
  });
}

function collectPresetData() {
  const ep = state.editingPreset;
  const resW = +getVal('p-res-w'), resH = +getVal('p-res-h');
  return {
    id: ep?.id || '',
    name: getVal('p-name'),
    resolution: { w: resW, h: resH },
    background: { type: getVal('p-bg-type'), color: getVal('p-bg-color'), file: state.tempPresetFiles.background_file || ep?.background?.file || null },
    bgm: { file: state.tempPresetFiles.bgm_file || ep?.bgm?.file || null, bgm_volume: +getVal('p-bgm-vol'), voice_volume: +getVal('p-voice-vol'), fade_out: +getVal('p-bgm-fade') },
    caption: {
      font: state.tempPresetFiles.font_file || ep?.caption?.font || null,
      text_color: getVal('p-text-color'),
      highlight: { mode: getVal('p-hi-mode'), color: getVal('p-hi-color'), pill_padding: 8 },
      without_visual: {
        font_size: +getVal('p-nv-size'), animation: getVal('p-nv-anim'),
        animation_speed: +getVal('p-nv-speed'),
        position: { x: 'center', y: 'center' }, max_chars_per_line: 22
      },
      with_visual: {
        font_size: +getVal('p-wv-size'), animation: getVal('p-wv-anim'),
        animation_speed: +getVal('p-wv-speed'), animation_duration: 0.4,
        position: { x: +getVal('p-wv-x'), y: +getVal('p-wv-y') }, max_chars_per_line: 28
      }
    },
    visual: {
      container: { x: +getVal('p-vis-x'), y: +getVal('p-vis-y'), w: +getVal('p-vis-w'), h: +getVal('p-vis-h') },
      crop_anchor: 'center', animation: 'fade_in', animation_duration: +getVal('p-vis-anim-dur')
    },
    frame: {
      enabled: document.getElementById('p-frame-enabled').checked,
      file: state.tempPresetFiles.frame_file || ep?.frame?.file || null,
      container: { x: +getVal('p-frame-x'), y: +getVal('p-frame-y'), w: +getVal('p-frame-w'), h: +getVal('p-frame-h') },
      crop_anchor: 'center'
    },
    transitions: { type: getVal('p-trans-type'), duration: +getVal('p-trans-dur') }
  };
}

async function savePreset() {
  const name = getVal('p-name').trim();
  if (!name) { toast('Please enter a preset name', 'error'); return; }
  const data = collectPresetData();
  const ep = state.editingPreset;
  try {
    let saved;
    if (ep?.id) {
      const r = await fetch(`/api/presets/${ep.id}`, { method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify(data) });
      saved = await r.json();
    } else {
      const r = await fetch('/api/presets', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(data) });
      saved = await r.json();
    }
    await loadPresets();
    renderPresetsGrid();
    populatePresetSelect();
    closePresetEditor();
    toast(`"${saved.name}" saved`, 'success');
  } catch { toast('Save failed', 'error'); }
}

async function confirmDeletePreset(id) {
  const p = state.presets.find(x => x.id === id);
  if (!confirm(`Delete preset "${p?.name}"?`)) return;
  await fetch(`/api/presets/${id}`, { method:'DELETE' });
  await loadPresets(); renderPresetsGrid(); populatePresetSelect();
  toast('Preset deleted');
}

async function sharePreset(id) {
  try {
    const r = await fetch(`/api/presets/${id}/share`, { method:'POST' });
    if (!r.ok) throw new Error();
    const { share_url } = await r.json();
    await navigator.clipboard.writeText(share_url);
    toast('Share link copied to clipboard', 'success');
  } catch { toast('Failed to copy share link', 'error'); }
}

function openImportPreset() {
  document.getElementById('import-preset-input').value = '';
  document.getElementById('import-preset-modal').style.display = 'flex';
  setTimeout(() => document.getElementById('import-preset-input').focus(), 50);
}

function closeImportPreset() {
  document.getElementById('import-preset-modal').style.display = 'none';
}

async function doImportPreset() {
  const token = document.getElementById('import-preset-input').value.trim();
  if (!token) {
    document.getElementById('import-preset-input').style.borderColor = 'var(--red)';
    return;
  }
  try {
    const r = await fetch('/api/presets/import', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token })
    });
    if (!r.ok) { toast('Share link not found', 'error'); return; }
    const p = await r.json();
    await loadPresets(); renderPresetsGrid(); populatePresetSelect();
    closeImportPreset();
    toast(`Imported "${p.name}"`, 'success');
  } catch { toast('Import failed', 'error'); }
}

function onBgTypeChange() {
  const type = getVal('p-bg-type');
  document.getElementById('bg-color-row').style.display = type==='color' ? 'block' : 'none';
  document.getElementById('bg-file-row').style.display = type!=='color' ? 'block' : 'none';
  ['vis','frame','text'].forEach(t => drawCanvas(t));
}

function onFrameToggle() {
  document.getElementById('frame-config').style.display = document.getElementById('p-frame-enabled').checked ? 'block' : 'none';
}

async function onPresetFileUpload(input, type, nameElId) {
  const file = input.files[0];
  if (!file) return;
  const fd = new FormData();
  fd.append('file', file);
  try {
    const r = await fetch(`/api/upload/${type}`, { method:'POST', body:fd });
    const data = await r.json();
    state.tempPresetFiles[`${type}_file`] = data.path;
    showFileSet(nameElId, file.name);
    toast(`${type} uploaded`, 'success');
    if (type === 'background' || type === 'frame') {
      delete canvasImageCache[mediaUrl(data.path)];
      ['vis','frame','text'].forEach(t => drawCanvas(t));
    }
  } catch { toast('Upload failed', 'error'); }
}

function clearPresetFile(nameElId, key) {
  const el = document.getElementById(nameElId);
  if (el) el.style.display = 'none';
  delete state.tempPresetFiles[key];
  if (key === 'background_file' || key === 'frame_file') {
    ['vis','frame','text'].forEach(t => drawCanvas(t));
  }
}

function showFileSet(elId, name) {
  const el = document.getElementById(elId);
  if (!el) return;
  el.style.display = 'flex';
  el.querySelector('.file-name').textContent = name;
}

// ─── Canvas drag-to-draw pickers ──────────────────────────────────────────────
const CANVAS_W = 180, CANVAS_H = 320;

const canvasImageCache = {};

function loadCanvasImage(url, callback) {
  if (canvasImageCache[url]) { callback(canvasImageCache[url]); return; }
  const img = new Image();
  img.onload = () => { canvasImageCache[url] = img; callback(img); };
  img.onerror = () => {};
  img.src = url;
}

function loadCanvasVideo(url, callback) {
  if (canvasImageCache[url]) { callback(canvasImageCache[url]); return; }
  const video = document.createElement('video');
  video.muted = true;
  video.src = url;
  video.addEventListener('loadeddata', () => { video.currentTime = 0.1; }, { once: true });
  video.addEventListener('seeked', () => { canvasImageCache[url] = video; callback(video); }, { once: true });
  video.load();
}

function drawImageCover(ctx, img, cw, ch) {
  const iw = img.videoWidth || img.naturalWidth || cw;
  const ih = img.videoHeight || img.naturalHeight || ch;
  const scale = Math.max(cw / iw, ch / ih);
  const sw = iw * scale, sh = ih * scale;
  ctx.drawImage(img, (cw - sw) / 2, (ch - sh) / 2, sw, sh);
}

function initCanvases() {
  ['vis','frame','text'].forEach(type => {
    const canvas = document.getElementById(`canvas-${type}`);
    if (!canvas) return;
    state.canvasDrag[type] = { dragging: false };
    canvas.onmousedown = e => startDraw(e, type);
    canvas.onmousemove = e => moveDraw(e, type);
    canvas.onmouseup = e => endDraw(e, type);
    canvas.onmouseleave = e => endDraw(e, type);
    drawCanvas(type);
  });
}

function getCanvasScale() {
  const w = +getVal('p-res-w') || 1080;
  const h = +getVal('p-res-h') || 1920;
  return { scaleX: w / CANVAS_W, scaleY: h / CANVAS_H, w, h };
}

function canvasPos(e, canvas) {
  const rect = canvas.getBoundingClientRect();
  return { x: e.clientX - rect.left, y: e.clientY - rect.top };
}

function startDraw(e, type) {
  const canvas = document.getElementById(`canvas-${type}`);
  const pos = canvasPos(e, canvas);
  state.canvasDrag[type] = { dragging: true, startX: pos.x, startY: pos.y, curX: pos.x, curY: pos.y };
}

function moveDraw(e, type) {
  if (!state.canvasDrag[type]?.dragging) return;
  const canvas = document.getElementById(`canvas-${type}`);
  const pos = canvasPos(e, canvas);
  state.canvasDrag[type].curX = pos.x;
  state.canvasDrag[type].curY = pos.y;
  drawCanvas(type);
}

function endDraw(e, type) {
  if (!state.canvasDrag[type]?.dragging) return;
  state.canvasDrag[type].dragging = false;
  const d = state.canvasDrag[type];
  const { scaleX, scaleY } = getCanvasScale();

  const x1 = Math.min(d.startX, d.curX), y1 = Math.min(d.startY, d.curY);
  const x2 = Math.max(d.startX, d.curX), y2 = Math.max(d.startY, d.curY);

  const px = Math.round(x1 * scaleX), py = Math.round(y1 * scaleY);
  const pw = Math.round((x2 - x1) * scaleX), ph = Math.round((y2 - y1) * scaleY);

  if (type === 'vis') {
    setVal('p-vis-x', px); setVal('p-vis-y', py);
    setVal('p-vis-w', pw); setVal('p-vis-h', ph);
  } else if (type === 'frame') {
    setVal('p-frame-x', px); setVal('p-frame-y', py);
    setVal('p-frame-w', pw); setVal('p-frame-h', ph);
  } else if (type === 'text') {
    // for text, record center of drawn box
    setVal('p-wv-x', Math.round((x1 + x2) / 2 * scaleX));
    setVal('p-wv-y', Math.round((y1 + y2) / 2 * scaleY));
  }
  drawCanvas(type);
}

function syncCanvasFromFields(type) {
  drawCanvas(type);
}

function drawCanvas(type) {
  const canvas = document.getElementById(`canvas-${type}`);
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  const { scaleX, scaleY } = getCanvasScale();
  ctx.clearRect(0, 0, CANVAS_W, CANVAS_H);

  const ep = state.editingPreset;
  const bgType = getVal('p-bg-type') || 'color';
  const bgColor = getVal('p-bg-color') || '#0a0a0a';
  const bgFile = state.tempPresetFiles?.background_file || ep?.background?.file || null;
  const frameFile = state.tempPresetFiles?.frame_file || ep?.frame?.file || null;

  // ── Phone background (clipped rounded rect) ──────────────────────────────
  ctx.save();
  ctx.beginPath();
  ctx.roundRect(0, 0, CANVAS_W, CANVAS_H, 6);
  ctx.clip();

  ctx.fillStyle = bgColor;
  ctx.fillRect(0, 0, CANVAS_W, CANVAS_H);

  if ((bgType === 'image' || bgType === 'video') && bgFile) {
    const url = mediaUrl(bgFile);
    const cached = canvasImageCache[url];
    if (cached) {
      drawImageCover(ctx, cached, CANVAS_W, CANVAS_H);
    } else if (bgType === 'image') {
      loadCanvasImage(url, () => drawCanvas(type));
    } else {
      loadCanvasVideo(url, () => drawCanvas(type));
    }
  }
  ctx.restore();

  // ── Shared values ─────────────────────────────────────────────────────────
  const visX = +getVal('p-vis-x') / scaleX, visY = +getVal('p-vis-y') / scaleY;
  const visW = +getVal('p-vis-w') / scaleX, visH = +getVal('p-vis-h') / scaleY;

  // ── Visual container reference on frame/text canvases ────────────────────
  if (type !== 'vis' && visW > 0 && visH > 0) {
    ctx.fillStyle = 'rgba(77,158,255,0.08)';
    ctx.fillRect(visX, visY, visW, visH);
    ctx.strokeStyle = 'rgba(77,158,255,0.35)';
    ctx.lineWidth = 1;
    ctx.setLineDash([3, 3]);
    ctx.strokeRect(visX, visY, visW, visH);
    ctx.setLineDash([]);
  }

  // ── Frame image overlay (frame canvas) ────────────────────────────────────
  // Media is center-cropped to full canvas; container area acts as the viewport
  if (type === 'frame' && document.getElementById('p-frame-enabled')?.checked && frameFile) {
    const url = mediaUrl(frameFile);
    const fx = +getVal('p-frame-x') / scaleX, fy = +getVal('p-frame-y') / scaleY;
    const fw = +getVal('p-frame-w') / scaleX, fh = +getVal('p-frame-h') / scaleY;
    const cached = canvasImageCache[url];
    if (cached && fw > 0 && fh > 0) {
      ctx.save();
      ctx.beginPath();
      ctx.rect(fx, fy, fw, fh);
      ctx.clip();
      drawImageCover(ctx, cached, CANVAS_W, CANVAS_H);
      ctx.restore();
    } else if (!cached) {
      const isVideo = /\.(mp4|webm|mov)$/i.test(frameFile);
      if (isVideo) loadCanvasVideo(url, () => drawCanvas(type));
      else loadCanvasImage(url, () => drawCanvas(type));
    }
  }

  // ── Main box for current type ─────────────────────────────────────────────
  let rx, ry, rw, rh;
  if (type === 'vis') {
    rx = visX; ry = visY; rw = visW; rh = visH;
    ctx.fillStyle = 'rgba(77,158,255,0.25)';
    ctx.strokeStyle = '#4d9eff';
  } else if (type === 'frame') {
    rx = +getVal('p-frame-x') / scaleX; ry = +getVal('p-frame-y') / scaleY;
    rw = +getVal('p-frame-w') / scaleX; rh = +getVal('p-frame-h') / scaleY;
    ctx.fillStyle = 'rgba(180,100,255,0.2)';
    ctx.strokeStyle = '#b464ff';
  } else {
    // text: crosshair at anchor point
    const cx = +getVal('p-wv-x') / scaleX, cy = +getVal('p-wv-y') / scaleY;
    ctx.strokeStyle = '#ff6b35';
    ctx.lineWidth = 1.5;
    ctx.beginPath(); ctx.moveTo(cx - 10, cy); ctx.lineTo(cx + 10, cy); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(cx, cy - 10); ctx.lineTo(cx, cy + 10); ctx.stroke();
    const d = state.canvasDrag[type];
    if (d?.dragging) {
      const x1 = Math.min(d.startX, d.curX), y1 = Math.min(d.startY, d.curY);
      ctx.strokeStyle = 'rgba(255,107,53,0.6)'; ctx.lineWidth = 1; ctx.setLineDash([3, 3]);
      ctx.strokeRect(x1, y1, Math.abs(d.curX - d.startX), Math.abs(d.curY - d.startY));
      ctx.setLineDash([]);
    }
    return;
  }

  if (rw > 0 && rh > 0) {
    ctx.fillRect(rx, ry, rw, rh);
    ctx.lineWidth = 1.5;
    ctx.strokeRect(rx, ry, rw, rh);
  }

  // ── Frame image on visual canvas ──────────────────────────────────────────
  if (type === 'vis' && document.getElementById('p-frame-enabled')?.checked && frameFile) {
    const url = mediaUrl(frameFile);
    const fx = +getVal('p-frame-x') / scaleX, fy = +getVal('p-frame-y') / scaleY;
    const fw = +getVal('p-frame-w') / scaleX, fh = +getVal('p-frame-h') / scaleY;
    const cached = canvasImageCache[url];
    if (cached && fw > 0 && fh > 0) {
      ctx.save();
      ctx.beginPath();
      ctx.rect(fx, fy, fw, fh);
      ctx.clip();
      drawImageCover(ctx, cached, CANVAS_W, CANVAS_H);
      ctx.restore();
    } else if (!cached) {
      const isVideo = /\.(mp4|webm|mov)$/i.test(frameFile);
      if (isVideo) loadCanvasVideo(url, () => drawCanvas(type));
      else loadCanvasImage(url, () => drawCanvas(type));
    }
  }

  // drag preview
  const d = state.canvasDrag[type];
  if (d?.dragging) {
    const x1 = Math.min(d.startX, d.curX), y1 = Math.min(d.startY, d.curY);
    ctx.fillStyle = type === 'vis' ? 'rgba(77,158,255,0.35)' : 'rgba(180,100,255,0.3)';
    ctx.fillRect(x1, y1, Math.abs(d.curX - d.startX), Math.abs(d.curY - d.startY));
    ctx.strokeStyle = type === 'vis' ? '#4d9eff' : '#b464ff';
    ctx.lineWidth = 1.5;
    ctx.strokeRect(x1, y1, Math.abs(d.curX - d.startX), Math.abs(d.curY - d.startY));
  }
}

// ─── Reels page ───────────────────────────────────────────────────────────────
async function loadReels() {
  try {
    const r = await fetch('/api/reels');
    const reels = await r.json();
    renderReelsGrid(reels);
  } catch { toast('Failed to load reels', 'error'); }
}

function renderReelsGrid(reels) {
  const grid = document.getElementById('reels-grid');
  const newCard = grid.querySelector('.new-reel-card');
  grid.innerHTML = '';
  grid.appendChild(newCard);

  reels.forEach(reel => {
    const card = document.createElement('div');
    card.className = 'reel-card';
    card.onclick = () => openReel(reel.id);
    const statusColor = reel.status === 'exported' ? 'var(--green)' : 'var(--muted)';
    const date = new Date(reel.updated_at).toLocaleString('en-IN', { day:'2-digit', month:'short', hour:'2-digit', minute:'2-digit' });
    card.innerHTML = `
      <div class="reel-card-name">${reel.name}</div>
      <div class="reel-card-meta">
        <span>${reel.preset_name || '—'}</span>
        <span>${reel.segments?.length || 0} segments</span>
        <span>${date}</span>
      </div>
      <div class="reel-card-status">
        <div class="status-dot" style="background:${statusColor}"></div>
        <span style="font-size:11px;color:${statusColor};font-family:'DM Mono',monospace">${reel.status}</span>
        ${reel.file_exists ? '<span style="font-size:11px;color:var(--muted);margin-left:4px">· ready to download</span>' : ''}
      </div>
      <div class="reel-card-actions">
        ${reel.file_exists ? `<button class="btn btn-ghost btn-sm" onclick="event.stopPropagation();window.location.href='/api/download?path=${encodeURIComponent(reel.last_output)}'">Download</button>` : ''}
        <button class="btn btn-danger btn-sm" onclick="event.stopPropagation();deleteReelConfirm(${reel.id})">Delete</button>
      </div>`;
    grid.appendChild(card);
  });
}

function newReelFlow() {
  // open editor fresh
  showPage('generate', document.querySelector('[data-page="generate"]'));
  resetEditor();
  // show name modal
  document.getElementById('reel-name-input').value = '';
  document.getElementById('name-modal').classList.add('show');
}

function confirmReelName() {
  const name = document.getElementById('reel-name-input').value.trim();
  if (!name) { toast('Please enter a name', 'error'); return; }
  state.reelName = name;
  document.getElementById('name-modal').classList.remove('show');
  document.getElementById('editor-reel-title').innerHTML = `Editor <span>/ ${name}</span>`;
  toast(`Reel "${name}" started`);
}

function startEditReelName() {
  if (!state.reelName) return; // no reel open yet
  const el = document.getElementById('editor-reel-title');
  if (el.querySelector('input')) return; // already editing
  el.innerHTML = `Editor <input id="reel-name-edit" type="text" value="${state.reelName}" style="font:inherit;background:var(--surface2);border:1px solid var(--accent);border-radius:5px;padding:1px 7px;color:var(--text);outline:none;width:180px;font-size:13px;font-weight:400"/>`;
  const input = el.querySelector('input');
  input.focus(); input.select();
  const commit = async () => {
    const name = input.value.trim();
    if (name && name !== state.reelName) {
      state.reelName = name;
      await saveReelDraft(true);
    }
    el.innerHTML = `Editor <span>/ ${state.reelName}</span>`;
  };
  input.onblur = commit;
  input.onkeydown = e => {
    if (e.key === 'Enter') { e.preventDefault(); input.blur(); }
    if (e.key === 'Escape') { el.innerHTML = `Editor <span>/ ${state.reelName}</span>`; }
  };
}

async function openReel(reelId) {
  try {
    const r = await fetch(`/api/reels/${reelId}`);
    const reel = await r.json();
    state.reelId = reel.id;
    state.reelName = reel.name;
    state.selectedPresetId = reel.preset_id;
    state.audioPath = reel.audio_path;
    state.introPath = reel.intro_path;
    state.outroPath = reel.outro_path;
    state.segments = reel.segments || [];

    document.getElementById('editor-reel-title').innerHTML = `Editor <span>/ ${reel.name}</span>`;
    setVal('preset-select', reel.preset_id);

    // show file sets
    if (reel.audio_path) {
      document.getElementById('audio-upload-zone').style.display = 'none';
      document.getElementById('audio-file-set').style.display = 'flex';
      document.getElementById('audio-file-name').textContent = reel.audio_path.split('/').pop();
    }
    if (reel.intro_path) {
      document.getElementById('intro-upload-zone').style.display = 'none';
      document.getElementById('intro-file-set').style.display = 'flex';
      document.getElementById('intro-file-name').textContent = reel.intro_path.split('/').pop();
    }
    if (reel.outro_path) {
      document.getElementById('outro-upload-zone').style.display = 'none';
      document.getElementById('outro-file-set').style.display = 'flex';
      document.getElementById('outro-file-name').textContent = reel.outro_path.split('/').pop();
    }

    if (reel.audio_path) {
      state.rawAudioPath = reel.audio_path;
      state.silenceReady = true;   // already processed in a prior session
      document.getElementById('silence-section').style.display = 'flex';
      document.getElementById('silence-stats').innerHTML =
        `<div class="stat-row"><span class="stat-label">Previously cleaned</span><span style="color:var(--muted)">re-run to adjust</span></div>`;
      document.getElementById('silence-stats').style.display = 'block';
      document.getElementById('silence-run-btn').textContent = 'Re-run';
    }

    if (state.segments.length) {
      renderSegments();
      document.getElementById('export-btn').disabled = false;
      setStep(4);
    } else if (state.silenceReady) {
      setStep(3);
    } else if (state.audioPath) {
      setStep(2);
    }

    checkTranscribeReady();
    showPage('generate', document.querySelector('[data-page="generate"]'));
    document.querySelector('[data-page="generate"]').classList.add('active');
    document.querySelector('[data-page="reels"]').classList.remove('active');
    document.querySelectorAll('.nav-item').forEach(n => n.classList.toggle('active', n.dataset.page==='generate'));
  } catch(e) { toast('Failed to open reel', 'error'); }
}

async function saveReelDraft(silent = false) {
  if (!state.reelName) { if (!silent) toast('No reel name set', 'error'); return; }
  if (!state.audioPath) { if (!silent) toast('Upload voiceover first', 'error'); return; }
  if (!state.selectedPresetId) { if (!silent) toast('Select a preset first', 'error'); return; }
  try {
    const body = {
      name: state.reelName,
      preset_id: state.selectedPresetId,
      audio_path: state.audioPath,
      intro_path: state.introPath,
      outro_path: state.outroPath,
      segments: state.segments
    };
    if (state.reelId) {
      const r = await fetch(`/api/reels/${state.reelId}`, { method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body) });
      const updated = await r.json();
      state.reelId = updated.id;
    } else {
      const r = await fetch('/api/reels', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body) });
      const created = await r.json();
      state.reelId = created.id;
    }
    if (!silent) toast('Draft saved', 'success');
  } catch { if (!silent) toast('Save failed', 'error'); }
}

async function deleteReelConfirm(id) {
  if (!confirm('Delete this reel? All associated files will be removed.')) return;
  await fetch(`/api/reels/${id}`, { method:'DELETE' });
  await loadReels();
  toast('Reel deleted');
}

// ─── Editor: uploads ──────────────────────────────────────────────────────────
function onPresetChange() {
  state.selectedPresetId = getVal('preset-select');
  checkTranscribeReady();
}

async function onAudioUpload(input) {
  const file = input.files[0]; if (!file) return;
  const fd = new FormData(); fd.append('file', file);
  try {
    const r = await fetch('/api/reel/upload/audio', { method:'POST', body:fd });
    const data = await r.json();
    state.audioPath = data.path;
    state.rawAudioPath = data.path;
    state.silenceReady = false;
    document.getElementById('audio-upload-zone').style.display = 'none';
    document.getElementById('audio-file-set').style.display = 'flex';
    document.getElementById('audio-file-name').textContent = file.name;
    document.getElementById('silence-section').style.display = 'flex';
    document.getElementById('silence-stats').style.display = 'none';
    document.getElementById('silence-run-btn').textContent = 'Remove Silences';
    setStep(2);
    checkTranscribeReady();
    toast('Audio uploaded', 'success');
  } catch { toast('Upload failed', 'error'); }
}

function clearAudio() {
  state.audioPath = null;
  state.rawAudioPath = null;
  state.silenceReady = false;
  document.getElementById('audio-upload-zone').style.display = 'block';
  document.getElementById('audio-file-set').style.display = 'none';
  document.getElementById('silence-section').style.display = 'none';
  document.getElementById('silence-stats').style.display = 'none';
  setStep(1);
  checkTranscribeReady();
}

async function onIntroUpload(input) {
  const file = input.files[0]; if (!file) return;
  const fd = new FormData(); fd.append('file', file);
  try {
    const r = await fetch('/api/reel/upload/intro', { method:'POST', body:fd });
    const data = await r.json();
    state.introPath = data.path;
    document.getElementById('intro-upload-zone').style.display = 'none';
    document.getElementById('intro-file-set').style.display = 'flex';
    document.getElementById('intro-file-name').textContent = file.name;
    toast('Intro uploaded', 'success');
  } catch { toast('Upload failed', 'error'); }
}

function clearIntro() {
  state.introPath = null;
  document.getElementById('intro-upload-zone').style.display = 'block';
  document.getElementById('intro-file-set').style.display = 'none';
}

async function onOutroUpload(input) {
  const file = input.files[0]; if (!file) return;
  const fd = new FormData(); fd.append('file', file);
  try {
    const r = await fetch('/api/reel/upload/outro', { method:'POST', body:fd });
    const data = await r.json();
    state.outroPath = data.path;
    document.getElementById('outro-upload-zone').style.display = 'none';
    document.getElementById('outro-file-set').style.display = 'flex';
    document.getElementById('outro-file-name').textContent = file.name;
    toast('Outro uploaded', 'success');
  } catch { toast('Upload failed', 'error'); }
}

function clearOutro() {
  state.outroPath = null;
  document.getElementById('outro-upload-zone').style.display = 'block';
  document.getElementById('outro-file-set').style.display = 'none';
}

function checkTranscribeReady() {
  document.getElementById('transcribe-btn').disabled =
    !(state.audioPath && state.selectedPresetId && state.silenceReady);
}

// ─── Silence removal ──────────────────────────────────────────────────────────
async function doRemoveSilence() {
  const btn = document.getElementById('silence-run-btn');
  btn.disabled = true;
  btn.innerHTML = '<div class="spinner"></div> Processing...';

  const fd = new FormData();
  fd.append('audio_path', state.rawAudioPath);
  fd.append('min_silence_s', document.getElementById('sil-min-len').value);
  fd.append('padding_s', document.getElementById('sil-padding').value);

  try {
    const r = await fetch('/api/reel/remove-silence', { method: 'POST', body: fd });
    if (!r.ok) throw new Error(await r.text());
    const data = await r.json();

    state.audioPath = data.cleaned_path;
    state.silenceReady = true;

    // Show stats
    const saved = data.original_duration - data.cleaned_duration;
    const pct   = data.original_duration > 0
      ? Math.round((saved / data.original_duration) * 100) : 0;
    const statsEl = document.getElementById('silence-stats');
    statsEl.style.display = 'block';
    if (data.removed_count === 0) {
      statsEl.innerHTML = `<div class="stat-row"><span class="stat-label">No silences found</span><span style="color:var(--muted)">audio unchanged</span></div>`;
    } else {
      statsEl.innerHTML = `
        <div class="stat-row"><span class="stat-label">Silences removed</span><span class="stat-removed">${data.removed_count}</span></div>
        <div class="stat-row"><span class="stat-label">Duration</span><span>${fmtDuration(data.original_duration)} → ${fmtDuration(data.cleaned_duration)}</span></div>
        <div class="stat-row"><span class="stat-label">Saved</span><span class="stat-saving">${fmtDuration(saved)} (${pct}%)</span></div>`;
    }

    setStep(3);
    checkTranscribeReady();
    toast(data.removed_count ? `${data.removed_count} silences removed` : 'No silences found', 'success');
  } catch(e) {
    toast('Silence removal failed: ' + e.message, 'error');
  } finally {
    btn.disabled = false;
    btn.innerHTML = 'Re-run';
  }
}

function skipSilenceRemoval() {
  state.audioPath = state.rawAudioPath;
  state.silenceReady = true;
  document.getElementById('silence-stats').style.display = 'block';
  document.getElementById('silence-stats').innerHTML =
    `<div class="stat-row"><span class="stat-label">Skipped</span><span style="color:var(--muted)">using original audio</span></div>`;
  document.getElementById('silence-run-btn').textContent = 'Re-run';
  setStep(3);
  checkTranscribeReady();
}

// ─── Transcription ────────────────────────────────────────────────────────────
async function doTranscribe() {
  const btn = document.getElementById('transcribe-btn');
  btn.disabled = true;
  btn.innerHTML = '<div class="spinner"></div> Transcribing...';
  const fd = new FormData();
  fd.append('audio_path', state.audioPath);
  try {
    const r = await fetch('/api/transcribe', { method:'POST', body:fd });
    if (!r.ok) throw new Error(await r.text());
    const data = await r.json();
    state.segments = data.segments.map(s => ({
      show_caption: true, mute_audio: false, ...s
    }));
    renderSegments();
    setStep(4);
    document.getElementById('export-btn').disabled = false;
    toast(`${state.segments.length} segments`, 'success');
    // auto-save draft
    await saveReelDraft();
  } catch(e) { toast('Transcription failed: ' + e.message, 'error'); }
  finally {
    btn.disabled = false;
    btn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z"/></svg> Transcribe Audio';
  }
}

// ─── Segments ─────────────────────────────────────────────────────────────────
function updateTotalDuration() {
  const el = document.getElementById('total-duration');
  if (!el) return;
  if (!state.segments.length) { el.style.display = 'none'; return; }
  const secs = state.segments.reduce((sum, s) => sum + Math.max(0, s.end - s.start), 0);
  el.textContent = fmtDuration(secs);
  el.style.display = '';
}

function renderSegments() {
  stopAudioPlayback();
  const list = document.getElementById('segments-list');
  list.innerHTML = '';
  document.getElementById('seg-count').textContent = `${state.segments.length} segments`;
  updateTotalDuration();
  state.segments.forEach((seg, i) => list.appendChild(buildSegCard(seg, i)));
  const hasSegs = state.segments.length > 0;
  const addBtn = document.getElementById('add-seg-btn');
  if (addBtn) addBtn.style.display = hasSegs ? 'inline-flex' : 'none';
  const mainPlayBtn = document.getElementById('main-play-btn');
  if (mainPlayBtn) mainPlayBtn.style.display = hasSegs ? 'inline-flex' : 'none';
}

const _PLAY_ICON = `<svg width="9" height="10" viewBox="0 0 9 10" fill="currentColor"><polygon points="0,0 9,5 0,10"/></svg>`;
const _STOP_ICON = `<svg width="9" height="10" viewBox="0 0 9 10" fill="currentColor"><rect x="0" y="0" width="3" height="10"/><rect x="6" y="0" width="3" height="10"/></svg>`;

const _DRAG_HANDLE = `<svg width="8" height="14" viewBox="0 0 8 14" fill="currentColor"><circle cx="2" cy="2" r="1.2"/><circle cx="6" cy="2" r="1.2"/><circle cx="2" cy="6" r="1.2"/><circle cx="6" cy="6" r="1.2"/><circle cx="2" cy="10" r="1.2"/><circle cx="6" cy="10" r="1.2"/></svg>`;

function buildSegCard(seg, i) {
  const card = document.createElement('div');
  card.className = `seg-card${seg.type==='visual'?' has-visual':''}${seg.is_intro_pin?' intro-pinned':''}${state.mainPlaying && state.playingIndex === i ? ' main-playing' : ''}`;
  card.id = `seg-card-${i}`;
  card.draggable = false;
  card.ondragstart = e => { if (card.draggable) startDragSeg(e, i); else e.preventDefault(); };
  card.ondragover = e => overDragSeg(e, i);
  card.ondrop = e => dropDragSeg(e, i);
  card.ondragend = () => { card.draggable = false; endDragSeg(); };
  const isPlaying = !state.mainPlaying && state.playingIndex === i;
  const badges = `
    <span class="badge ${seg.type==='visual'?'badge-visual':'badge-text'}">${seg.type==='visual'?'visual':'text'}</span>
    ${seg.is_intro_pin?'<span class="badge badge-intro">intro</span>':''}
  `;
  card.innerHTML = `
    <div class="seg-card-top">
      <span class="seg-drag-handle" title="Drag to reorder">${_DRAG_HANDLE}</span>
      <span class="seg-index">${String(i+1).padStart(2,'0')}</span>
      <button class="seg-play-btn${isPlaying?' playing':''}" id="play-btn-${i}" onclick="playSegmentAudio(${i})" title="Preview audio">
        ${isPlaying ? _STOP_ICON : _PLAY_ICON}
      </button>
      <span class="seg-time">
        <span class="seg-time-val" id="seg-start-${i}" onclick="startEditTime(${i},'start')" title="Click to edit start time">${fmtTime(seg.start)}</span>
        <span class="seg-time-sep">–</span>
        <span class="seg-time-val" id="seg-end-${i}" onclick="startEditTime(${i},'end')" title="Click to edit end time">${fmtTime(seg.end)}</span>
      </span>
      <div class="seg-badges">${badges}</div>
      <button class="btn btn-danger btn-sm seg-delete-btn" onclick="deleteSegment(${i})" title="Delete segment">✕</button>
    </div>
    ${(seg.show_caption !== false) ? `
    <div class="seg-caption-wrap">
      <div class="seg-caption" id="caption-${i}" onclick="startEditCaption(${i}, event)">${renderHighlights(seg.caption)}</div>
    </div>` : ''}
    <div class="seg-actions">
      ${seg.type==='visual'
        ? `<button class="btn btn-ghost btn-sm" onclick="setSegType(${i},'text_only')">Remove Visual</button>`
        : `<button class="btn btn-secondary btn-sm" onclick="setSegType(${i},'visual')">+ Add Visual</button>`}
      ${seg.is_intro_pin
        ? `<button class="btn btn-ghost btn-sm" onclick="toggleIntroPin(${i})">Unpin Intro</button>`
        : `<button class="btn btn-ghost btn-sm" onclick="toggleIntroPin(${i})">Pin Intro</button>`}
      <button class="btn btn-ghost btn-sm" onclick="addSegmentAfter(${i})" style="margin-left:auto">+ Add Below</button>
    </div>
    <div class="seg-toggles">
      <label class="seg-toggle"><input type="checkbox" ${seg.show_caption !== false ? 'checked' : ''} onchange="toggleSegField(${i},'show_caption',this.checked)"> Caption</label>
      <label class="seg-toggle"><input type="checkbox" ${seg.mute_audio ? 'checked' : ''} onchange="toggleSegField(${i},'mute_audio',this.checked)"> Mute audio</label>
    </div>
    ${seg.type==='visual' ? buildVisualSlot(seg, i) : ''}`;
  // Only allow drag when initiated from the handle
  const handle = card.querySelector('.seg-drag-handle');
  if (handle) {
    handle.addEventListener('mousedown', () => { card.draggable = true; });
    document.addEventListener('mouseup', () => { card.draggable = false; }, { once: true });
  }
  return card;
}

function buildVisualSlot(seg, i) {
  if (seg.visual_path) {
    const isVideo = /\.(mp4|mov|webm|avi)$/i.test(seg.visual_path);
    const isFit = seg.visual_mode === 'fit';
    return `<div class="seg-visual-preview">
      ${isVideo ? `<video src="${mediaUrl(seg.visual_path)}" autoplay muted loop playsinline style="object-fit:${isFit?'contain':'cover'}"></video>` : `<img src="${mediaUrl(seg.visual_path)}" style="object-fit:${isFit?'contain':'cover'}"/>`}
      <div class="remove-vis" onclick="clearSegVisual(${i})">✕</div>
      <div class="vis-mode-toggle">
        <button class="${!isFit?'active':''}" onclick="setVisualMode(${i},'crop')" title="Center crop">Crop</button>
        <button class="${isFit?'active':''}" onclick="setVisualMode(${i},'fit')" title="Center fit">Fit</button>
      </div>
    </div>`;
  }
  return `<div class="seg-upload-zone">
    <input type="file" accept="image/*,video/*" onchange="onSegVisualUpload(this,${i})"/>
    <p>Drop visual here or <strong>click to upload</strong></p>
  </div>`;
}

function setVisualMode(i, mode) {
  pushHistory();
  state.segments[i].visual_mode = mode;
  refreshCard(i);
  saveReelDraft(true);
}

function renderHighlights(caption) {
  return caption.replace(/\*([^*]+)\*/g, '<span class="hi">$1</span>');
}

// Returns the total character offset of (targetNode, targetOffset) within root
function getTextOffset(root, targetNode, targetOffset) {
  let offset = 0;
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  while (walker.nextNode()) {
    const node = walker.currentNode;
    if (node === targetNode) return offset + targetOffset;
    offset += node.textContent.length;
  }
  return offset;
}

// Maps a visible character offset (in stripped text) back to raw caption offset
// (accounting for highlight markers like *word*)
function visibleToRawOffset(caption, visibleOffset) {
  let vis = 0, raw = 0;
  while (raw < caption.length && vis < visibleOffset) {
    if (caption[raw] === '*') { raw++; continue; }
    vis++; raw++;
  }
  // Skip any trailing asterisk at cursor position
  while (raw < caption.length && caption[raw] === '*') raw++;
  return raw;
}

function startEditCaption(i, event) {
  const el = document.getElementById(`caption-${i}`);
  if (el.contentEditable === 'true') return;

  // Capture click position in the rendered (highlighted) DOM before we replace it
  let targetRawOffset = null;
  if (event) {
    const caretRange = document.caretRangeFromPoint
      ? document.caretRangeFromPoint(event.clientX, event.clientY)
      : (document.caretPositionFromPoint
          ? (() => { const p = document.caretPositionFromPoint(event.clientX, event.clientY); return p ? { startContainer: p.offsetNode, startOffset: p.offset } : null; })()
          : null);
    if (caretRange) {
      const visibleOffset = getTextOffset(el, caretRange.startContainer, caretRange.startOffset);
      targetRawOffset = visibleToRawOffset(state.segments[i].caption, visibleOffset);
    }
  }

  pushHistory();
  const caption = state.segments[i].caption;
  el.textContent = caption;
  el.contentEditable = 'true';
  el.focus();

  // Place cursor at click position (or end if no event)
  const sel = window.getSelection();
  sel.removeAllRanges();
  const range = document.createRange();
  const textNode = el.firstChild;
  if (textNode && targetRawOffset !== null) {
    const safeOffset = Math.min(targetRawOffset, textNode.textContent.length);
    range.setStart(textNode, safeOffset);
    range.collapse(true);
  } else {
    range.selectNodeContents(el);
    range.collapse(false);
  }
  sel.addRange(range);

  el.onblur = () => {
    state.segments[i].caption = el.textContent;
    el.contentEditable = 'false';
    el.innerHTML = renderHighlights(el.textContent);
  };
  el.onkeydown = e => {
    if (e.key==='Enter' && !e.shiftKey) { e.preventDefault(); el.blur(); }
    if (e.key==='Escape') { el.textContent=state.segments[i].caption; el.contentEditable='false'; el.innerHTML=renderHighlights(state.segments[i].caption); }
    if (e.key==='b' && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      const sel = window.getSelection();
      if (!sel.rangeCount) return;
      const r = sel.getRangeAt(0);
      if (r.collapsed) return;
      const selected = r.toString();
      if (!selected) return;
      // Replace selected text with *selected*
      r.deleteContents();
      const wrapped = document.createTextNode(`*${selected}*`);
      r.insertNode(wrapped);
      // Move cursor to after the inserted text
      const newRange = document.createRange();
      newRange.setStartAfter(wrapped);
      newRange.collapse(true);
      sel.removeAllRanges();
      sel.addRange(newRange);
    }
  };
}

function setSegType(i, type) {
  pushHistory();
  state.segments[i].type = type;
  if (type==='text_only') state.segments[i].visual_path = null;
  refreshCard(i);
}

function toggleIntroPin(i) {
  pushHistory();
  state.segments.forEach((s,j) => { if(j!==i) s.is_intro_pin=false; });
  state.segments[i].is_intro_pin = !state.segments[i].is_intro_pin;
  renderSegments();
}

function toggleSegField(i, field, value) {
  pushHistory();
  state.segments[i][field] = value;
  refreshCard(i);
}

async function onSegVisualUpload(input, i) {
  const file = input.files[0]; if (!file) return;
  const fd = new FormData();
  fd.append('file', file);
  fd.append('segment_index', i);
  try {
    const r = await fetch('/api/reel/upload/visual', { method:'POST', body:fd });
    const data = await r.json();
    pushHistory();
    state.segments[i].visual_path = data.path;
    state.segments[i].type = 'visual';
    refreshCard(i);
    toast('Visual uploaded', 'success');
  } catch { toast('Upload failed', 'error'); }
}

function clearSegVisual(i) {
  pushHistory();
  state.segments[i].visual_path = null;
  state.segments[i].type = 'text_only';
  refreshCard(i);
}

function refreshCard(i) {
  const old = document.getElementById(`seg-card-${i}`);
  old.replaceWith(buildSegCard(state.segments[i], i));
}

// ─── Segment audio preview ────────────────────────────────────────────────────

function _getPresetBgm() {
  const p = state.presets.find(x => x.id === state.selectedPresetId);
  return p?.bgm || null;
}

function _stopBgm(withFade) {
  if (state.bgmFadeTimer) { clearInterval(state.bgmFadeTimer); state.bgmFadeTimer = null; }
  const bgm = state.bgmPlayer;
  if (!bgm) return;
  const fadeDur = withFade ? (_getPresetBgm()?.fade_out ?? 2) : 0;
  if (fadeDur > 0) {
    const steps = 30;
    const delta = bgm.volume / steps;
    state.bgmFadeTimer = setInterval(() => {
      bgm.volume = Math.max(0, bgm.volume - delta);
      if (bgm.volume <= 0) {
        clearInterval(state.bgmFadeTimer);
        state.bgmFadeTimer = null;
        bgm.pause();
        if (state.bgmPlayer === bgm) state.bgmPlayer = null;
      }
    }, (fadeDur * 1000) / steps);
  } else {
    bgm.pause();
    state.bgmPlayer = null;
  }
}

function stopAudioPlayback() {
  _stopBgm(false);
  if (state.segTrackInterval) { clearInterval(state.segTrackInterval); state.segTrackInterval = null; }
  if (state.audioPlayer) {
    state.audioPlayer.pause();
    state.audioPlayer.ontimeupdate = null;
    state.audioPlayer = null;
  }
  if (state.playingIndex !== null) {
    const btn = document.getElementById(`play-btn-${state.playingIndex}`);
    if (btn) { btn.classList.remove('playing'); btn.innerHTML = _PLAY_ICON; }
    state.playingIndex = null;
  }
  if (state.mainPlaying || state.mainPaused) {
    state.mainPlaying = false;
    state.mainPaused = false;
    state.mainPausedIndex = null;
    document.querySelectorAll('.seg-card.main-playing, .seg-card.main-paused-at').forEach(c =>
      c.classList.remove('main-playing', 'main-paused-at'));
    updateMainPlayBtn();
  }
}

function playSegmentAudio(i) {
  if (!state.audioPath) { toast('No audio loaded', 'error'); return; }
  if (state.mainPlaying) {
    // Clicking the currently-playing segment's button → pause
    if (state.playingIndex === i) { pauseMainPlay(); return; }
    // Clicking a different segment → jump to it
    const audio = state.audioPlayer;
    if (state.segTrackInterval) { clearInterval(state.segTrackInterval); state.segTrackInterval = null; }
    audio.ontimeupdate = null;
    audio.currentTime = state.segments[i].start;
    audio.addEventListener('seeked', () => {
      if (state.audioPlayer !== audio) return;
      activateSegmentTracking(audio, i);
    }, { once: true });
    return;
  }
  if (state.mainPaused) { resumeMainPlay(i); return; }
  startMainPlay(i);
}

// ─── Main play-all ────────────────────────────────────────────────────────────

function updateMainPlayBtn() {
  const btn = document.getElementById('main-play-btn');
  if (!btn) return;
  btn.classList.toggle('playing', state.mainPlaying);
  btn.classList.toggle('paused', state.mainPaused);
  if (state.mainPlaying) btn.innerHTML = `&#9646;&#9646; Pause`;
  else if (state.mainPaused) btn.innerHTML = `&#9654; Resume`;
  else btn.innerHTML = `&#9654; Play All`;
}

function toggleMainPlay() {
  if (state.mainPlaying) pauseMainPlay();
  else if (state.mainPaused) resumeMainPlay(state.mainPausedIndex);
  else startMainPlay(0);
}

// fromIndex: which segment to start from (default 0)
function startMainPlay(fromIndex) {
  const i = fromIndex ?? 0;
  if (!state.audioPath) { toast('No audio loaded', 'error'); return; }
  if (!state.segments.length) { toast('No segments', 'error'); return; }
  stopAudioPlayback();
  state.mainPlaying = true;
  state.mainPaused = false;
  state.mainPausedIndex = null;
  updateMainPlayBtn();

  const bgmCfg = _getPresetBgm();
  const audio = new Audio(mediaUrl(state.audioPath));
  audio.volume = bgmCfg?.voice_volume ?? 1;
  state.audioPlayer = audio;

  // BGM player
  if (bgmCfg?.file) {
    const bgm = new Audio(mediaUrl(bgmCfg.file));
    bgm.loop = true;
    bgm.volume = bgmCfg.bgm_volume ?? 0.15;
    state.bgmPlayer = bgm;
    const startTime = state.segments[i]?.start ?? 0;
    bgm.addEventListener('loadedmetadata', () => {
      if (state.bgmPlayer !== bgm) return;
      bgm.currentTime = bgm.duration ? startTime % bgm.duration : 0;
      bgm.play().catch(() => {});
    }, { once: true });
  }

  audio.addEventListener('loadedmetadata', () => {
    const startTime = state.segments[i]?.start ?? 0;
    const doPlay = () => {
      if (state.audioPlayer !== audio) return; // superseded
      activateSegmentTracking(audio, i);
      audio.play().catch(() => { stopAudioPlayback(); toast('Audio playback failed', 'error'); });
    };
    if (startTime === 0) {
      doPlay();
    } else {
      // Must wait for seeked — setting currentTime is async; calling play()
      // immediately would start from 0 while the seek is still in progress
      audio.currentTime = startTime;
      audio.addEventListener('seeked', doPlay, { once: true });
    }
  }, { once: true });
}

function pauseMainPlay() {
  if (!state.mainPlaying) return;
  if (state.segTrackInterval) { clearInterval(state.segTrackInterval); state.segTrackInterval = null; }
  if (state.bgmPlayer) state.bgmPlayer.pause();
  if (state.audioPlayer) {
    state.audioPlayer.pause();
    state.audioPlayer.ontimeupdate = null;
  }
  // Reset the active segment's play button back to play icon
  if (state.playingIndex !== null) {
    const btn = document.getElementById(`play-btn-${state.playingIndex}`);
    if (btn) { btn.classList.remove('playing'); btn.innerHTML = _PLAY_ICON; }
  }
  state.mainPausedIndex = state.playingIndex;
  state.mainPlaying = false;
  state.mainPaused = true;
  updateMainPlayBtn();
  document.querySelectorAll('.seg-card.main-playing').forEach(c => {
    c.classList.remove('main-playing');
    c.classList.add('main-paused-at');
  });
}

function resumeMainPlay(fromIndex) {
  const i = fromIndex ?? state.mainPausedIndex ?? 0;
  document.querySelectorAll('.seg-card.main-paused-at').forEach(c => c.classList.remove('main-paused-at'));
  state.mainPlaying = true;
  state.mainPaused = false;
  state.mainPausedIndex = null;
  updateMainPlayBtn();
  const audio = state.audioPlayer || new Audio(mediaUrl(state.audioPath));
  state.audioPlayer = audio;
  const startTime = state.segments[i]?.start ?? 0;
  if (state.bgmPlayer) state.bgmPlayer.play().catch(() => {});
  const doResume = () => {
    if (state.audioPlayer !== audio) return;
    activateSegmentTracking(audio, i);
    audio.play().catch(() => stopAudioPlayback());
  };
  if (Math.abs(audio.currentTime - startTime) < 0.05) {
    doResume();
  } else {
    audio.currentTime = startTime;
    audio.addEventListener('seeked', doResume, { once: true });
  }
}

// Sets up UI highlight + 50ms interval boundary for segment i.
// Does NOT call play() — caller is responsible for that.
// When segment ends, seeks to next segment's start and recurses.
function activateSegmentTracking(audio, i) {
  if (!state.mainPlaying) return;
  if (i >= state.segments.length) { stopAudioPlayback(); return; }

  // Clear any previous tracker
  if (state.segTrackInterval) { clearInterval(state.segTrackInterval); state.segTrackInterval = null; }
  audio.ontimeupdate = null;

  // Reset previous segment's play button
  if (state.playingIndex !== null && state.playingIndex !== i) {
    const prevBtn = document.getElementById(`play-btn-${state.playingIndex}`);
    if (prevBtn) { prevBtn.classList.remove('playing'); prevBtn.innerHTML = _PLAY_ICON; }
  }

  state.playingIndex = i;

  // Respect mute_audio flag — silence the voice track for muted segments
  audio.muted = !!state.segments[i]?.mute_audio;

  document.querySelectorAll('.seg-card.main-playing, .seg-card.main-paused-at').forEach(c =>
    c.classList.remove('main-playing', 'main-paused-at'));
  const card = document.getElementById(`seg-card-${i}`);
  if (card) {
    card.classList.add('main-playing');
    card.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }

  // Show pause icon on the active segment's play button
  const activeBtn = document.getElementById(`play-btn-${i}`);
  if (activeBtn) { activeBtn.classList.add('playing'); activeBtn.innerHTML = _STOP_ICON; }

  const advance = () => {
    if (state.audioPlayer !== audio || !state.mainPlaying) return;
    if (state.segTrackInterval) { clearInterval(state.segTrackInterval); state.segTrackInterval = null; }
    // Reset current segment's play button
    const btn = document.getElementById(`play-btn-${i}`);
    if (btn) { btn.classList.remove('playing'); btn.innerHTML = _PLAY_ICON; }
    if (i + 1 < state.segments.length) {
      const nextStart = state.segments[i + 1].start;
      audio.currentTime = nextStart;
      audio.addEventListener('seeked', () => {
        if (state.audioPlayer !== audio) return;
        activateSegmentTracking(audio, i + 1);
      }, { once: true });
    } else {
      // Last segment ended naturally — fade BGM out, stop voice without cancelling the fade
      _stopBgm(true);
      audio.pause();
      audio.ontimeupdate = null;
      if (state.audioPlayer === audio) state.audioPlayer = null;
      state.playingIndex = null;
      state.mainPlaying = false;
      state.mainPaused = false;
      state.mainPausedIndex = null;
      document.querySelectorAll('.seg-card.main-playing, .seg-card.main-paused-at').forEach(c =>
        c.classList.remove('main-playing', 'main-paused-at'));
      updateMainPlayBtn();
    }
  };

  // Poll at 50ms for precise end-time detection (vs coarse ontimeupdate ~4×/sec)
  state.segTrackInterval = setInterval(() => {
    if (state.audioPlayer !== audio || !state.mainPlaying) {
      clearInterval(state.segTrackInterval); state.segTrackInterval = null; return;
    }
    const liveSeg = state.segments[i];
    if (!liveSeg || audio.currentTime >= liveSeg.end) advance();
  }, 50);
}

// ─── Drag to reorder ──────────────────────────────────────────────────────────

function startDragSeg(e, i) {
  state.dragSrcIndex = i;
  e.dataTransfer.effectAllowed = 'move';
  e.dataTransfer.setData('text/plain', i);
  setTimeout(() => document.getElementById(`seg-card-${i}`)?.classList.add('dragging'), 0);
}

function overDragSeg(e, i) {
  e.preventDefault();
  e.dataTransfer.dropEffect = 'move';
  document.querySelectorAll('.seg-card').forEach(c => c.classList.remove('drag-over-top', 'drag-over-bottom'));
  if (i === state.dragSrcIndex) return;
  const card = document.getElementById(`seg-card-${i}`);
  if (card) {
    const rect = card.getBoundingClientRect();
    card.classList.add(e.clientY < rect.top + rect.height / 2 ? 'drag-over-top' : 'drag-over-bottom');
  }
}

function dropDragSeg(e, i) {
  e.preventDefault();
  const card = document.getElementById(`seg-card-${i}`);
  const isTop = card && e.clientY < card.getBoundingClientRect().top + card.getBoundingClientRect().height / 2;
  document.querySelectorAll('.seg-card').forEach(c => c.classList.remove('drag-over-top', 'drag-over-bottom', 'dragging'));
  const from = state.dragSrcIndex;
  state.dragSrcIndex = null;
  if (from === null || from === i) return;
  pushHistory();
  let to = isTop ? i : i + 1;
  if (from < to) to -= 1;
  const [moved] = state.segments.splice(from, 1);
  state.segments.splice(to, 0, moved);
  state.segments.forEach((s, j) => { s.index = j; });
  renderSegments();
}

function endDragSeg() {
  document.querySelectorAll('.seg-card').forEach(c => c.classList.remove('drag-over-top', 'drag-over-bottom', 'dragging'));
  state.dragSrcIndex = null;
}

// ─── Segment time editing ─────────────────────────────────────────────────────

function startEditTime(i, field) {
  const el = document.getElementById(`seg-${field}-${i}`);
  if (!el || el.contentEditable === 'true') return;
  pushHistory();
  const saved = state.segments[i][field];
  el.textContent = saved.toFixed(2);
  el.contentEditable = 'true';
  el.focus();
  const range = document.createRange();
  range.selectNodeContents(el); range.collapse(false);
  window.getSelection().removeAllRanges();
  window.getSelection().addRange(range);

  const commit = () => {
    const val = parseFloat(el.textContent);
    if (!isNaN(val) && val >= 0) state.segments[i][field] = Math.round(val * 100) / 100;
    el.contentEditable = 'false';
    el.textContent = fmtTime(state.segments[i][field]);
    updateTotalDuration();
    saveReelDraft(true);
  };
  el.onblur = commit;
  el.onkeydown = e => {
    if (e.key === 'Enter') { e.preventDefault(); el.blur(); }
    if (e.key === 'Escape') { el.contentEditable = 'false'; el.textContent = fmtTime(saved); }
  };
}

// ─── Add / Delete segments ────────────────────────────────────────────────────

function deleteSegment(i) {
  pushHistory();
  state.segments.splice(i, 1);
  state.segments.forEach((s, j) => { s.index = j; });
  renderSegments();
}

function addSegmentAfter(i) {
  pushHistory();
  const prev = state.segments[i];
  const next = state.segments[i + 1];
  const newStart = prev ? Math.round(prev.end * 100) / 100 : 0;
  const newEnd = next ? Math.min(next.start, newStart + 2) : newStart + 2;
  state.segments.splice(i + 1, 0, {
    index: i + 1,
    start: newStart,
    end: Math.round(newEnd * 100) / 100,
    caption: '',
    type: 'text_only',
    visual_path: null,
    is_intro_pin: false, show_caption: true, mute_audio: false
  });
  state.segments.forEach((s, j) => { s.index = j; });
  renderSegments();
  setTimeout(() => startEditCaption(i + 1), 50);
}

function addSegmentAtEnd() {
  if (state.segments.length === 0) {
    pushHistory();
    state.segments.push({ index: 0, start: 0, end: 2, caption: '', type: 'text_only', visual_path: null, is_intro_pin: false, show_caption: true, mute_audio: false });
    renderSegments();
    setTimeout(() => startEditCaption(0), 50);
  } else {
    addSegmentAfter(state.segments.length - 1);
  }
}

// ─── Export ───────────────────────────────────────────────────────────────────
async function startExport() {
  if (!state.reelId) {
    await saveReelDraft();
    if (!state.reelId) { toast('Save draft first', 'error'); return; }
  }
  // auto-save segments before render
  await fetch(`/api/reels/${state.reelId}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ segments: state.segments, intro_path: state.introPath, outro_path: state.outroPath })
  });

  const overlay = document.getElementById('export-overlay');
  overlay.classList.add('show');
  document.getElementById('export-download').style.display = 'none';
  document.getElementById('export-cancel').style.display = 'block';
  resetExportSteps();
  setExportProgress(5);
  setExportStep('audio', 'active');

  let es = null;

  try {
    // Start background render job
    const r = await fetch(`/api/reels/${state.reelId}/render`, { method: 'POST' });
    if (r.status === 402) {
      overlay.classList.remove('show');
      const err = await r.json();
      const detail = err.detail || err;
      showInsufficientCredits(detail.balance ?? 0, detail.required ?? 0, detail.shortfall ?? 0);
      return;
    }
    if (!r.ok) { const e = await r.json(); throw new Error(e.detail || 'Render failed'); }
    const { job_id } = await r.json();

    setExportStep('audio', 'done');
    setExportProgress(10);

    await new Promise((resolve, reject) => {
      es = new EventSource(`/api/reels/${state.reelId}/render-stream/${job_id}`);

      es.onmessage = (evt) => {
        const msg = JSON.parse(evt.data);

        if (msg.type === 'progress') {
          const { step, total, stage, message } = msg;
          // Map stage to UI step
          if (stage === 'render') {
            setExportStep('render', 'active');
            document.getElementById('render-step-label').textContent = `Rendering — ${message}`;
            // progress: 10% to 75% over all segments
            setExportProgress(10 + Math.round((step / total) * 65));
          } else if (stage === 'concat') {
            setExportStep('render', 'done');
            setExportStep('concat', 'active');
            setExportProgress(78);
          } else if (stage === 'bgm') {
            setExportStep('concat', 'done');
            setExportStep('bgm', 'active');
            setExportProgress(88);
          }
        } else if (msg.type === 'done') {
          es.close();
          setExportStep('concat', 'done');
          setExportStep('bgm', 'done');
          setExportStep('export', 'active');
          setExportProgress(100);
          setTimeout(() => setExportStep('export', 'done'), 200);
          document.getElementById('export-cancel').style.display = 'none';
          document.getElementById('export-download').style.display = 'block';
          document.getElementById('download-btn').onclick = () => {
            window.location.href = `/api/download?path=${encodeURIComponent(msg.output_path)}`;
          };
          setStep(5);
          if (msg.credits_deducted > 0)
            toast(`Export complete! ${msg.credits_deducted} credits used.`, 'success');
          else
            toast('Export complete!', 'success');
          loadCreditBalance();
          resolve();
        } else if (msg.type === 'error') {
          es.close();
          reject(new Error(msg.message));
        }
      };

      es.onerror = () => {
        es.close();
        reject(new Error('Stream connection lost'));
      };
    });

  } catch (e) {
    if (es) es.close();
    toast('Export failed: ' + e.message, 'error');
    overlay.classList.remove('show');
  }
}

function cancelExport() { document.getElementById('export-overlay').classList.remove('show'); }

function resetExportSteps() {
  document.querySelectorAll('.export-step').forEach(s => s.classList.remove('active','done'));
}
function setExportStep(name, st) {
  const el = document.querySelector(`.export-step[data-step="${name}"]`);
  if (el) { el.classList.remove('active','done'); if(st) el.classList.add(st); }
}
function setExportProgress(pct) { document.getElementById('export-bar').style.width = pct+'%'; }

// ─── Steps ────────────────────────────────────────────────────────────────────
function setStep(n) {
  state.step = n;
  for (let i=1;i<=5;i++) {
    const el = document.getElementById(`step-${i}`);
    if (!el) continue;
    el.classList.remove('active','done');
    if (i<n) el.classList.add('done');
    else if (i===n) el.classList.add('active');
  }
}

function resetEditor() {
  state.reelId = null; state.reelName = null;
  state.audioPath = null; state.rawAudioPath = null; state.silenceReady = false;
  state.introPath = null; state.outroPath = null;
  state.segments = []; state.selectedPresetId = null;
  setVal('preset-select','');
  ['audio','intro','outro'].forEach(t => {
    document.getElementById(`${t}-upload-zone`).style.display='block';
    document.getElementById(`${t}-file-set`).style.display='none';
  });
  document.getElementById('silence-section').style.display = 'none';
  document.getElementById('silence-stats').style.display = 'none';
  document.getElementById('silence-run-btn').textContent = 'Remove Silences';
  document.getElementById('segments-list').innerHTML = `<div class="empty-state">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M9 9h6M9 12h6M9 15h4"/></svg>
    <p>Upload a voiceover and click <strong>Transcribe Audio</strong></p>
  </div>`;
  document.getElementById('seg-count').textContent = '0 segments';
  document.getElementById('export-btn').disabled = true;
  document.getElementById('transcribe-btn').disabled = true;
  document.getElementById('editor-reel-title').innerHTML = 'Editor <span>/ New Reel</span>';
  setStep(1);
}

// ─── Credits & Billing ────────────────────────────────────────────────────────

let _creditConfig = { price_per_credit: 0.5, min_recharge: 100 };

async function loadCreditBalance() {
  try {
    const r = await fetch('/api/credits/balance');
    if (!r.ok) return;
    const data = await r.json();
    _creditConfig.price_per_credit = data.price_per_credit;
    _creditConfig.min_recharge     = data.min_recharge;
    _creditConfig.user_type        = data.user_type;
    _creditConfig.balance          = data.balance;

    const chip = document.getElementById('credits-sidebar');
    const val  = document.getElementById('sidebar-credits');
    const btn = document.getElementById('credits-chip-btn');
    if (chip && val) {
      if (data.user_type === 'pro') {
        chip.style.display = 'block';
        btn.innerHTML = '<div style="text-align: center;width: 100%;">You are a <span class="pro-badge">PRO</span></div>';
      } else {
        chip.style.display = 'block';
        val.textContent = data.balance.toLocaleString();
      }
    }
  } catch {}
}

function openRechargeModal(prefillCredits) {
  const inp = document.getElementById('recharge-credits-input');
  inp.value = prefillCredits || Math.max(_creditConfig.min_recharge, 500);
  updateRechargePreview();
  document.getElementById('recharge-modal').classList.add('show');
}

function closeRechargeModal() {
  document.getElementById('recharge-modal').classList.remove('show');
}

function updateRechargePreview() {
  const credits = Math.max(0, parseInt(document.getElementById('recharge-credits-input').value) || 0);
  const price   = _creditConfig.price_per_credit;
  const normal  = credits * 1;            // 1 rupee per credit (original)
  const actual  = credits * price;        // current offer
  const mins    = Math.floor(credits / 60);
  const secs    = credits % 60;

  document.getElementById('recharge-price-val').textContent   = `₹${actual.toFixed(2)}`;
  document.getElementById('recharge-price-orig').textContent  = `₹${normal.toFixed(2)}`;
  document.getElementById('recharge-credits-preview').textContent = credits.toLocaleString();
  document.getElementById('recharge-mins-preview').textContent =
    mins > 0 ? `${mins}m ${secs}s` : `${secs}s`;
}

async function doRecharge() {
  const credits = parseInt(document.getElementById('recharge-credits-input').value) || 0;
  if (credits < _creditConfig.min_recharge) {
    toast(`Minimum recharge is ${_creditConfig.min_recharge} credits`, 'error'); return;
  }
  document.getElementById('recharge-pay-btn').disabled = true;
  try {
    const r = await fetch('/api/payments/create-order', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ credits }),
    });
    if (!r.ok) { const e = await r.json(); throw new Error(e.detail || 'Order creation failed'); }
    const order = await r.json();

    const options = {
      key:         order.key_id,
      amount:      order.amount,
      currency:    order.currency,
      name:        'ReelD',
      description: `${credits} Credits`,
      order_id:    order.order_id,
      handler: async (response) => {
        try {
          const vr = await fetch('/api/payments/verify', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              order_id:   response.razorpay_order_id,
              payment_id: response.razorpay_payment_id,
              signature:  response.razorpay_signature,
              credits,
            }),
          });
          if (!vr.ok) throw new Error('Verification failed');
          const result = await vr.json();
          closeRechargeModal();
          await loadCreditBalance();
          toast(`✅ ${result.credits_added} credits added! Balance: ${result.balance}`, 'success');
          // If there was a pending export, retry it
          if (_pendingExportAfterRecharge) {
            _pendingExportAfterRecharge = false;
            document.getElementById('insufficient-modal').classList.remove('show');
            setTimeout(startExport, 300);
          }
        } catch (e) { toast('Payment verification failed', 'error'); }
      },
      prefill: { name: state.currentUser?.name || '', email: state.currentUser?.email || '' },
      theme: { color: '#ff6b35' },
    };
    const rzp = new Razorpay(options);
    rzp.on('payment.failed', () => toast('Payment failed. Please try again.', 'error'));
    rzp.open();
  } catch (e) {
    toast(e.message, 'error');
  } finally {
    document.getElementById('recharge-pay-btn').disabled = false;
  }
}

let _pendingExportAfterRecharge = false;

function showInsufficientCredits(balance, required, shortfall) {
  document.getElementById('ins-balance').textContent  = `${balance} credits`;
  document.getElementById('ins-required').textContent = `${required} credits`;
  document.getElementById('ins-shortfall').textContent = `${shortfall} credits`;
  document.getElementById('insufficient-modal').classList.add('show');
}

function openRechargeFromInsufficient() {
  _pendingExportAfterRecharge = true;
  const shortfall = parseInt(document.getElementById('ins-shortfall').textContent) || 0;
  const suggested = Math.max(_creditConfig.min_recharge, Math.ceil(shortfall / 100) * 100);
  document.getElementById('insufficient-modal').classList.remove('show');
  openRechargeModal(suggested);
}

// ─── Billing page ─────────────────────────────────────────────────────────────

async function loadBillingPage() {
  // Update stat cards
  const balance   = _creditConfig.balance ?? '—';
  const userType  = _creditConfig.user_type ?? 'free';
  const price     = _creditConfig.price_per_credit ?? 0.5;

  document.getElementById('billing-balance').textContent = typeof balance === 'number' ? balance.toLocaleString() : balance;
  const typeEl = document.getElementById('billing-account-type');
  if (userType === 'pro') {
    typeEl.innerHTML = '<span class="pro-badge" style="font-size:14px">PRO</span>';
    document.getElementById('billing-price-note').textContent = 'Unlimited exports included';
  } else {
    typeEl.textContent = 'Free';
    document.getElementById('billing-price-note').textContent = `₹${price.toFixed(2)} per credit (50% off)`;
  }

  // Load transactions
  const body = document.getElementById('billing-txn-body');
  body.innerHTML = '<div style="padding:32px;text-align:center;color:var(--muted);font-size:13px">Loading...</div>';
  try {
    const r = await fetch('/api/credits/transactions');
    const txns = await r.json();
    document.getElementById('billing-txn-count').textContent = `${txns.length} transactions`;
    if (!txns.length) {
      body.innerHTML = '<div style="padding:32px;text-align:center;color:var(--muted);font-size:13px">No transactions yet</div>';
      return;
    }
    let running = 0;
    const allAmounts = txns.slice().reverse().map(t => t.amount);
    const runningBals = [];
    let rb = 0;
    for (const a of allAmounts) { rb += a; runningBals.push(rb); }
    runningBals.reverse();

    const rows = txns.map((t, idx) => {
      const isCredit = t.amount > 0;
      const amtStr   = isCredit ? `+${t.amount}` : `${t.amount}`;
      const date     = new Date(t.created_at).toLocaleString('en-IN', {dateStyle:'medium', timeStyle:'short'});
      const printBtn = isCredit
        ? `<button class="btn btn-ghost btn-sm" onclick="printInvoice(this)" data-txn='${JSON.stringify(t).replace(/'/g,'&#39;')}'>🖨 Print</button>`
        : '';
      return `<tr>
        <td>${date}</td>
        <td><span class="txn-badge ${isCredit ? 'recharge' : 'deduction'}">${isCredit ? 'Recharge' : 'Export'}</span></td>
        <td style="max-width:260px;color:var(--muted)">${t.description || '—'}</td>
        <td class="${isCredit ? 'txn-credit' : 'txn-debit'}">${amtStr}</td>
        <td style="font-family:'DM Mono',monospace;color:var(--muted)">${runningBals[idx]}</td>
        <td>${printBtn}</td>
      </tr>`;
    }).join('');

    body.innerHTML = `<table class="txn-table">
      <thead><tr><th>Date</th><th>Type</th><th>Description</th><th>Credits</th><th>Balance After</th><th></th></tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
  } catch (e) {
    body.innerHTML = `<div style="padding:32px;text-align:center;color:#ff8080">Failed to load transactions</div>`;
  }
}

function printInvoice(btn) {
  const t = JSON.parse(btn.dataset.txn);
  const date = new Date(t.created_at).toLocaleString('en-IN', {dateStyle:'long', timeStyle:'short'});
  const rupees = (t.amount * (_creditConfig.price_per_credit ?? 0.5)).toFixed(2);

  document.getElementById('print-inv-meta').innerHTML =
    `Payment ID: ${t.razorpay_payment_id || 'N/A'} &nbsp;|&nbsp; Order ID: ${t.razorpay_order_id || 'N/A'} &nbsp;|&nbsp; Date: ${date}`;
  document.getElementById('print-inv-body').innerHTML =
    `<tr><td>${t.description || 'Credit Recharge'}</td><td>${t.amount}</td><td>₹${rupees}</td></tr>`;
  document.getElementById('print-inv-total').innerHTML =
    `Total Paid: <strong>₹${rupees}</strong>`;

  window.print();
}

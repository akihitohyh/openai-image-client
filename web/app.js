'use strict';

const STORAGE_KEY = 'openai-image-client-web:settings:v4';
const LEGACY_STORAGE_KEYS = ['openai-image-client-web:v3'];
const POLL_INTERVAL_MS = 1500;
const HISTORY_LIMIT = 12;
const OPENAI_MAX_REFERENCE_IMAGES = 16;
const GROK_MAX_REFERENCE_IMAGES = 3;
const GROK_MAX_VIDEO_REFERENCE_IMAGES = 7;
const MAX_REFERENCE_IMAGE_BYTES = 10 * 1024 * 1024;
const MAX_REFERENCE_IMAGES_TOTAL_BYTES = 30 * 1024 * 1024;
const ALLOWED_REFERENCE_IMAGE_TYPES = new Set(['image/png', 'image/jpeg', 'image/webp']);

const PROTOCOL_CONFIG = {
  'openai-image': {
    backendProtocol: 'openai',
    defaultModel: 'gpt-image-2',
    models: ['gpt-image-2'],
    description: 'OpenAI 生图使用 Images API，适合 gpt-image-2。',
    promptPlaceholder: '例如: A professional minimalist workspace, cinematic lighting...',
    promptLabel: '提示词 (Prompt)',
    generateLabel: '文生图 (Generate)',
    editLabel: '图生图 (Edit)',
    modeLabel: '作业模式',
    referenceLabel: '参考图 (OpenAI 最多 16 张)',
    uploadPlaceholder: '点击选择或追加参考图像',
    sizeLabel: '分辨率 (Size)',
    countLabel: '生成张数 (n)',
    timeoutLabel: '超时断开 (秒)',
    submitText: '发送生图请求',
    waitingText: '正在等待图片异步任务',
    maxReferenceImages: OPENAI_MAX_REFERENCE_IMAGES,
    maxOutputs: 8,
    isVideo: false,
  },
  'grok-image': {
    backendProtocol: 'grok',
    defaultModel: 'grok-imagine',
    models: ['grok-imagine', 'grok-imagine-edit', 'grok-imagine-image', 'grok-imagine-image-quality'],
    description: 'Grok 生图使用 Grok Imagine 图片协议，支持文生图和图生图。',
    promptPlaceholder: '例如: A cinematic futuristic city at sunset, neon reflections...',
    promptLabel: '提示词 (Prompt)',
    generateLabel: '文生图 (Generate)',
    editLabel: '图生图 (Edit)',
    modeLabel: '作业模式',
    referenceLabel: '参考图 (Grok 生图最多 3 张)',
    uploadPlaceholder: '点击选择或追加参考图像',
    sizeLabel: '分辨率 (Size)',
    countLabel: '生成张数 (n)',
    timeoutLabel: '超时断开 (秒)',
    submitText: '发送 Grok 生图请求',
    waitingText: '正在等待 Grok 图片异步任务',
    maxReferenceImages: GROK_MAX_REFERENCE_IMAGES,
    maxOutputs: 8,
    isVideo: false,
  },
  'grok-video': {
    backendProtocol: 'grok',
    defaultModel: 'grok-imagine-video-1.5',
    models: ['grok-imagine-video-1.5', 'grok-imagine-video'],
    description: 'Grok 视频会创建异步任务并轮询结果，支持文生视频和图生视频。',
    promptPlaceholder: '例如: A cinematic drone shot over a futuristic city at sunset, smooth camera movement...',
    promptLabel: '视频提示词 (Prompt)',
    generateLabel: '文生视频',
    editLabel: '图生视频',
    modeLabel: '视频模式',
    referenceLabel: '参考图 (Grok 视频最多 7 张)',
    uploadPlaceholder: '点击选择或追加视频参考图像',
    sizeLabel: '画幅参考 (Aspect)',
    countLabel: '生成视频数 (n)',
    timeoutLabel: '任务超时 (秒)',
    submitText: '发送 Grok 视频请求',
    waitingText: '正在等待 Grok 视频异步任务',
    maxReferenceImages: GROK_MAX_VIDEO_REFERENCE_IMAGES,
    maxOutputs: 4,
    isVideo: true,
  },
};

const DEFAULT_SETTINGS = {
  protocol: 'openai-image',
  mode: 'generate',
  baseUrl: '',
  model: 'gpt-image-2',
  size: '1024x1024',
  count: 1,
  timeout: 500,
  videoResolution: '720p',
  duration: 10,
};

const els = Object.fromEntries([
  'service-status', 'config-form', 'base_url', 'api_key', 'protocol', 'protocol-control',
  'protocol-desc', 'mode-control', 'mode-label', 'mode-generate', 'mode-edit',
  'edit-image-container', 'reference-label', 'reference-dropzone', 'file-input',
  'upload-placeholder', 'upload-preview-grid', 'upload-count', 'clear-upload-btn',
  'prompt-label', 'prompt', 'model', 'size-label', 'size', 'video-options',
  'video_resolution', 'duration', 'count-label', 'image_count', 'timeout-label',
  'timeout', 'submit-btn', 'job-status-desc', 'loader', 'cancel-btn',
  'clear-history-btn', 'diagnostic-panel', 'error-message', 'diagnostic-details',
  'meta-panel', 'meta-req-id', 'meta-duration', 'meta-wait', 'meta-endpoint',
  'meta-status', 'request-hint', 'request-message', 'request-elapsed', 'history-list',
  'lightbox', 'lightbox-backdrop', 'lightbox-title', 'lightbox-close',
  'lightbox-image', 'lightbox-video',
].map((id) => [id, document.getElementById(id)]));

let currentMode = 'generate';
let uploadedReferenceImages = [];
let activeTask = null;
let taskTimerId = null;
let historyEntries = [];
let lastFocusedElement = null;

function clampInt(value, min, max, fallback) {
  const number = Number(value);
  if (!Number.isFinite(number)) return fallback;
  return Math.min(max, Math.max(min, Math.round(number)));
}

function apiPath(path) {
  const cleanPath = path.startsWith('/') ? path : `/${path}`;
  const pathname = window.location.pathname;
  if (pathname.startsWith('/image.html') || pathname.startsWith('/image-client/')) {
    return `/image-client-api${cleanPath.replace(/^\/api/, '')}`;
  }
  return cleanPath;
}

function getSelectedProtocol() {
  const selected = els.protocol.value;
  return PROTOCOL_CONFIG[selected] ? selected : DEFAULT_SETTINGS.protocol;
}

function getProtocolConfig() {
  return PROTOCOL_CONFIG[getSelectedProtocol()];
}

function safePersistableBaseUrl(value) {
  const raw = String(value || '').trim();
  if (!raw) return '';
  try {
    const parsed = new URL(raw);
    if (!['http:', 'https:'].includes(parsed.protocol)) return '';
    if (parsed.username || parsed.password || parsed.search || parsed.hash) return '';
    return parsed.toString().replace(/\/$/, '');
  } catch {
    return '';
  }
}

function clearLegacySecrets() {
  try {
    for (const key of LEGACY_STORAGE_KEYS) {
      localStorage.removeItem(key);
    }
  } catch {
    // Storage can be unavailable in sandboxed or privacy-restricted iframes.
  }
}

function loadSettings() {
  clearLegacySecrets();
  try {
    const parsed = JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}');
    const protocol = PROTOCOL_CONFIG[parsed.protocol] ? parsed.protocol : DEFAULT_SETTINGS.protocol;
    const config = PROTOCOL_CONFIG[protocol];
    const model = config.models.includes(parsed.model) ? parsed.model : config.defaultModel;
    return {
      protocol,
      mode: parsed.mode === 'edit' ? 'edit' : 'generate',
      baseUrl: safePersistableBaseUrl(parsed.baseUrl),
      model,
      size: String(parsed.size || DEFAULT_SETTINGS.size),
      count: clampInt(parsed.count, 1, config.maxOutputs, DEFAULT_SETTINGS.count),
      timeout: clampInt(parsed.timeout, 5, 600, DEFAULT_SETTINGS.timeout),
      videoResolution: ['480p', '720p', '1080p'].includes(parsed.videoResolution)
        ? parsed.videoResolution
        : DEFAULT_SETTINGS.videoResolution,
      duration: clampInt(parsed.duration, 1, 15, DEFAULT_SETTINGS.duration),
    };
  } catch {
    return { ...DEFAULT_SETTINGS };
  }
}

function persistSettings() {
  const settings = {
    protocol: getSelectedProtocol(),
    mode: currentMode,
    baseUrl: safePersistableBaseUrl(els.base_url.value),
    model: els.model.value,
    size: els.size.value,
    count: clampInt(els.image_count.value, 1, getProtocolConfig().maxOutputs, 1),
    timeout: clampInt(els.timeout.value, 5, 600, 500),
    videoResolution: els.video_resolution.value,
    duration: clampInt(els.duration.value, 1, 15, 10),
  };
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(settings));
  } catch {
    // Settings persistence is optional; generation must remain usable without it.
  }
}

function formatElapsed(milliseconds) {
  const totalSeconds = Math.max(0, Math.floor(milliseconds / 1000));
  const minutes = String(Math.floor(totalSeconds / 60)).padStart(2, '0');
  const seconds = String(totalSeconds % 60).padStart(2, '0');
  return `${minutes}:${seconds}`;
}

function formatBytes(bytes) {
  const value = Number(bytes);
  if (!Number.isFinite(value) || value <= 0) return '0 B';
  if (value < 1024) return `${value} B`;
  if (value < 1024 ** 2) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 ** 2)).toFixed(1)} MB`;
}

function formatApiError(data) {
  if (!data || typeof data !== 'object') return '';
  const parts = [];
  const message = data.message || data.error?.message;
  if (message) parts.push(`错误：${message}`);
  if (data.last_endpoint) parts.push(`最后请求地址：${data.last_endpoint}`);
  if (data.client_request_id) parts.push(`客户端请求 ID：${data.client_request_id}`);
  if (Array.isArray(data.diagnostics) && data.diagnostics.length) {
    parts.push('尝试过的地址：', ...data.diagnostics);
  }
  return parts.join('\n');
}

function setHealthStatus(ok) {
  els['service-status'].textContent = ok ? '● 后端服务在线' : '● 后端连接失败';
  els['service-status'].classList.toggle('text-emerald-600', ok);
  els['service-status'].classList.toggle('font-medium', ok);
  els['service-status'].classList.toggle('text-rose-600', !ok);
}

async function checkHealth() {
  try {
    const response = await fetch(apiPath('/api/health'), { cache: 'no-store' });
    const data = await response.json().catch(() => ({}));
    setHealthStatus(response.ok && data.ok === true);
  } catch {
    setHealthStatus(false);
  }
}

function setStatus(text) {
  els['job-status-desc'].textContent = text;
}

function showError(title, details) {
  els['diagnostic-panel'].classList.remove('hidden');
  els['error-message'].textContent = title;
  els['diagnostic-details'].textContent = details || '无更深入的报错上下文';
}

function hideError() {
  els['diagnostic-panel'].classList.add('hidden');
  els['error-message'].textContent = '';
  els['diagnostic-details'].textContent = '';
}

function setLoadingState(isLoading) {
  els['submit-btn'].disabled = isLoading;
  els['submit-btn'].classList.toggle('opacity-50', isLoading);
  els['submit-btn'].classList.toggle('cursor-not-allowed', isLoading);
  els.loader.classList.toggle('hidden', !isLoading);
  els['cancel-btn'].classList.toggle('hidden', !isLoading);
  els['clear-history-btn'].disabled = isLoading;
  els['clear-history-btn'].classList.toggle('opacity-50', isLoading);
}

function updateProtocolButtons() {
  const selected = getSelectedProtocol();
  for (const button of els['protocol-control'].querySelectorAll('[data-protocol]')) {
    const active = button.dataset.protocol === selected;
    button.setAttribute('aria-pressed', String(active));
    button.className = active
      ? 'text-xs py-1.5 rounded-md font-medium text-center bg-white shadow-sm text-slate-800 transition'
      : 'text-xs py-1.5 rounded-md font-medium text-center text-slate-500 hover:text-slate-800 transition';
  }
}

function updateModeButtons() {
  for (const button of els['mode-control'].querySelectorAll('[data-mode]')) {
    const active = button.dataset.mode === currentMode;
    button.setAttribute('aria-pressed', String(active));
    button.className = active
      ? 'text-xs py-1.5 rounded-md font-medium text-center bg-white shadow-sm text-slate-800 transition'
      : 'text-xs py-1.5 rounded-md font-medium text-center text-slate-500 hover:text-slate-800 transition';
  }
  els['edit-image-container'].classList.toggle('hidden', currentMode !== 'edit');
}

function updateModelOptions(config, preferredModel) {
  const currentValue = preferredModel || els.model.value;
  els.model.replaceChildren();
  for (const modelName of config.models) {
    const option = document.createElement('option');
    option.value = modelName;
    option.textContent = modelName;
    els.model.append(option);
  }
  els.model.value = config.models.includes(currentValue) ? currentValue : config.defaultModel;
}

function applyProtocolPreset(preferredModel) {
  const config = getProtocolConfig();
  updateModelOptions(config, preferredModel);
  updateProtocolButtons();
  updateModeButtons();

  els['protocol-desc'].textContent = config.description;
  els['mode-label'].textContent = config.modeLabel;
  els['mode-generate'].textContent = config.generateLabel;
  els['mode-edit'].textContent = config.editLabel;
  els['reference-label'].textContent = config.referenceLabel;
  els['upload-placeholder'].textContent = config.uploadPlaceholder;
  els['prompt-label'].textContent = config.promptLabel;
  els.prompt.placeholder = config.promptPlaceholder;
  els['size-label'].textContent = config.sizeLabel;
  els['count-label'].textContent = config.countLabel;
  els.image_count.max = String(config.maxOutputs);
  els.image_count.value = String(clampInt(els.image_count.value, 1, config.maxOutputs, 1));
  els['timeout-label'].textContent = config.timeoutLabel;
  els['video-options'].classList.toggle('hidden', !config.isVideo);
  els['submit-btn'].querySelector('span').textContent = config.submitText;
  els['request-message'].textContent = config.waitingText;

  if (uploadedReferenceImages.length > config.maxReferenceImages) {
    uploadedReferenceImages = uploadedReferenceImages.slice(0, config.maxReferenceImages);
    setStatus(`协议已切换，仅保留前 ${config.maxReferenceImages} 张参考图。`);
  }
  renderReferenceImages();
}

function switchProtocol(protocol, preferredModel) {
  if (!PROTOCOL_CONFIG[protocol] || activeTask) return;
  els.protocol.value = protocol;
  applyProtocolPreset(preferredModel);
  persistSettings();
}

function switchMode(mode) {
  if (activeTask) return;
  currentMode = mode === 'edit' ? 'edit' : 'generate';
  updateModeButtons();
  persistSettings();
}

function readFileAsDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ''));
    reader.onerror = () => reject(new Error('读取参考图失败，请重新选择'));
    reader.readAsDataURL(file);
  });
}

async function addReferenceFiles(files) {
  const accepted = [];
  const warnings = [];
  let totalBytes = uploadedReferenceImages.reduce((sum, image) => sum + Number(image.size || 0), 0);
  for (const file of files) {
    if (!ALLOWED_REFERENCE_IMAGE_TYPES.has(file.type)) {
      warnings.push(`${file.name || '未命名文件'}：仅支持 PNG / JPG / WEBP`);
    } else if (file.size > MAX_REFERENCE_IMAGE_BYTES) {
      warnings.push(`${file.name || '未命名文件'}：单张不能超过 10 MB`);
    } else if (totalBytes + file.size > MAX_REFERENCE_IMAGES_TOTAL_BYTES) {
      warnings.push(`${file.name || '未命名文件'}：全部参考图合计不能超过 30 MB`);
    } else {
      accepted.push(file);
      totalBytes += file.size;
    }
  }

  const maximum = getProtocolConfig().maxReferenceImages;
  const remaining = Math.max(0, maximum - uploadedReferenceImages.length);
  if (accepted.length > remaining) {
    warnings.push(`当前协议最多上传 ${maximum} 张，仅保留本次前 ${remaining} 张`);
  }

  for (const file of accepted.slice(0, remaining)) {
    uploadedReferenceImages.push({
      name: file.name || 'reference-image',
      content_type: file.type || 'image/png',
      size: file.size,
      data_url: await readFileAsDataUrl(file),
    });
  }
  renderReferenceImages();
  if (warnings.length) showError('部分参考图未添加', warnings.join('\n'));
}

function removeReferenceImage(index) {
  uploadedReferenceImages.splice(index, 1);
  renderReferenceImages();
}

function clearReferenceImages() {
  uploadedReferenceImages = [];
  els['file-input'].value = '';
  renderReferenceImages();
}

function createReferenceCard(reference, index) {
  const card = document.createElement('div');
  card.className = 'relative aspect-square overflow-hidden rounded border border-slate-200 bg-slate-100 shadow-sm';
  card.title = `${reference.name} · ${formatBytes(reference.size)}`;

  const preview = document.createElement('button');
  preview.type = 'button';
  preview.className = 'absolute inset-0 z-10 focus:outline-none focus:ring-2 focus:ring-inset focus:ring-slate-500';
  preview.setAttribute('aria-label', `预览参考图 ${index + 1}`);
  preview.addEventListener('click', (event) => {
    event.stopPropagation();
    openLightbox('image', reference.data_url, `参考图 #${index + 1}`);
  });

  const image = document.createElement('img');
  image.src = reference.data_url;
  image.alt = reference.name || `参考图 ${index + 1}`;
  image.className = 'h-full w-full object-cover';

  const badge = document.createElement('span');
  badge.className = 'absolute left-1 top-1 z-20 rounded bg-white/90 px-1.5 py-0.5 text-[10px] text-slate-700 shadow';
  badge.textContent = `#${index + 1}`;

  const remove = document.createElement('button');
  remove.type = 'button';
  remove.className = 'absolute right-1 top-1 z-20 rounded bg-slate-900/80 px-1.5 py-0.5 text-[10px] text-white hover:bg-rose-600 transition';
  remove.textContent = '移除';
  remove.setAttribute('aria-label', `移除参考图 ${index + 1}`);
  remove.addEventListener('click', (event) => {
    event.stopPropagation();
    removeReferenceImage(index);
  });

  card.append(preview, image, badge, remove);
  return card;
}

function renderReferenceImages() {
  const hasImages = uploadedReferenceImages.length > 0;
  const maximum = getProtocolConfig().maxReferenceImages;
  els['upload-placeholder'].classList.toggle('hidden', hasImages);
  els['upload-preview-grid'].classList.toggle('hidden', !hasImages);
  els['clear-upload-btn'].classList.toggle('hidden', !hasImages);
  els['upload-count'].textContent = hasImages
    ? `已选择 ${uploadedReferenceImages.length} / ${maximum} 张参考图`
    : '尚未选择图片';
  els['upload-preview-grid'].replaceChildren(
    ...uploadedReferenceImages.map((reference, index) => createReferenceCard(reference, index)),
  );
}

function startTaskTimer(entry) {
  stopTaskTimer();
  const tick = () => {
    entry.elapsedMs = Date.now() - entry.startedAt;
    const elapsedText = formatElapsed(entry.elapsedMs);
    els['request-elapsed'].textContent = elapsedText;
    els['meta-wait'].textContent = elapsedText;
    const historyElapsed = els['history-list'].querySelector(`[data-entry-id="${entry.id}"] [data-entry-elapsed]`);
    if (historyElapsed) historyElapsed.textContent = elapsedText;
  };
  tick();
  taskTimerId = window.setInterval(tick, 1000);
}

function stopTaskTimer() {
  if (taskTimerId !== null) {
    window.clearInterval(taskTimerId);
    taskTimerId = null;
  }
}

function statusPresentation(status) {
  if (status === 'success') return { text: '已完成', classes: 'bg-emerald-100 text-emerald-700' };
  if (status === 'error') return { text: '失败', classes: 'bg-rose-100 text-rose-700' };
  if (status === 'cancelled') return { text: '已取消', classes: 'bg-slate-200 text-slate-600' };
  if (status === 'queued') return { text: '排队中', classes: 'bg-amber-100 text-amber-700' };
  return { text: '处理中', classes: 'bg-sky-100 text-sky-700' };
}

function mediaExtension(source, type) {
  const match = String(source).match(/^data:image\/([a-z0-9.+-]+);/i);
  if (match) return match[1] === 'jpeg' ? 'jpg' : match[1].replace('svg+xml', 'svg');
  try {
    const pathname = new URL(String(source), window.location.href).pathname;
    const extension = pathname.match(/\.([a-z0-9]{2,5})$/i)?.[1]?.toLowerCase();
    if (type === 'video' && ['mp4', 'webm'].includes(extension)) return extension;
    if (type === 'image' && ['png', 'jpg', 'jpeg', 'webp', 'gif'].includes(extension)) {
      return extension === 'jpeg' ? 'jpg' : extension;
    }
  } catch {
    // Fall through to a media-appropriate default.
  }
  return type === 'video' ? 'mp4' : 'png';
}

function createMediaCard(source, type, index, entry) {
  const wrapper = document.createElement('div');
  wrapper.className = type === 'video'
    ? 'relative border border-slate-200 rounded-lg overflow-hidden bg-slate-950 aspect-video'
    : 'relative border border-slate-200 rounded-lg overflow-hidden bg-slate-100 aspect-square';

  const media = document.createElement(type === 'video' ? 'video' : 'img');
  media.src = source;
  media.className = type === 'video' ? 'w-full h-full object-contain bg-slate-950' : 'w-full h-full object-cover';
  if (type === 'video') {
    media.controls = true;
    media.preload = 'metadata';
    media.playsInline = true;
  } else {
    media.alt = `生成图像 ${index + 1}`;
    media.referrerPolicy = 'no-referrer';
  }

  const actions = document.createElement('div');
  actions.className = 'absolute inset-x-0 bottom-0 z-20 bg-slate-900/60 text-white text-xs px-3 py-2 flex items-center justify-between gap-2';

  const preview = document.createElement('button');
  preview.type = 'button';
  preview.className = 'truncate text-left hover:underline focus:outline-none focus:ring-2 focus:ring-white';
  preview.textContent = type === 'video' ? `预览视频 #${index + 1}` : `预览图像 #${index + 1}`;
  preview.addEventListener('click', () => openLightbox(type, source, `${type === 'video' ? '视频' : '图像'} #${index + 1}`));

  const download = document.createElement('a');
  download.className = 'bg-white/90 hover:bg-white text-slate-800 text-xs px-2.5 py-1 rounded shadow font-medium transition shrink-0';
  download.href = source;
  download.download = `${type}_${entry.createdAt}_${index + 1}.${mediaExtension(source, type)}`;
  download.textContent = '下载';

  actions.append(preview, download);
  wrapper.append(media, actions);
  return wrapper;
}

function createHistoryEntry(entry) {
  const article = document.createElement('article');
  article.dataset.entryId = entry.id;
  article.className = 'border border-slate-200 rounded-lg bg-white p-4 shadow-sm animate-fade-in';

  const header = document.createElement('div');
  header.className = 'flex items-start justify-between gap-3';
  const summary = document.createElement('div');
  summary.className = 'min-w-0';
  const title = document.createElement('h3');
  title.className = 'text-sm font-semibold text-slate-800';
  title.textContent = `${PROTOCOL_CONFIG[entry.protocol].isVideo ? '视频' : '图像'}任务 · ${entry.mode === 'edit' ? '参考图模式' : '生成模式'}`;
  const metadata = document.createElement('p');
  metadata.className = 'mt-1 text-[11px] text-slate-400 break-all';
  metadata.append(`${[entry.model, entry.size, `n=${entry.count}`].filter(Boolean).join(' · ')} · `);
  const elapsed = document.createElement('span');
  elapsed.dataset.entryElapsed = '';
  elapsed.textContent = formatElapsed(entry.elapsedMs || 0);
  metadata.append(elapsed);
  if (entry.jobId) metadata.append(` · Task ${entry.jobId}`);
  summary.append(title, metadata);

  const presentation = statusPresentation(entry.status);
  const status = document.createElement('span');
  status.className = `shrink-0 rounded px-2 py-1 text-[11px] font-semibold ${presentation.classes}`;
  status.textContent = presentation.text;
  header.append(summary, status);

  const prompt = document.createElement('p');
  prompt.className = 'mt-3 text-xs text-slate-600 whitespace-pre-wrap break-words';
  prompt.textContent = entry.prompt;
  article.append(header, prompt);

  if (entry.status === 'queued' || entry.status === 'running') {
    const pending = document.createElement('div');
    pending.className = 'mt-4 border border-dashed border-sky-200 bg-sky-50/50 rounded-lg min-h-28 flex items-center justify-center text-xs text-sky-700';
    pending.textContent = '异步任务处理中，结果会自动更新';
    article.append(pending);
  } else if (entry.status === 'error' || entry.status === 'cancelled') {
    const message = document.createElement('pre');
    message.className = entry.status === 'error'
      ? 'mt-4 whitespace-pre-wrap text-xs text-rose-700 bg-rose-50 border border-rose-100 rounded p-3'
      : 'mt-4 whitespace-pre-wrap text-xs text-slate-600 bg-slate-50 border border-slate-200 rounded p-3';
    message.textContent = entry.errorMessage || (entry.status === 'cancelled' ? '任务已取消。' : '任务失败。');
    article.append(message);
  } else {
    const mediaItems = [
      ...entry.images.map((source) => ({ source, type: 'image' })),
      ...entry.videos.map((source) => ({ source, type: 'video' })),
    ];
    const grid = document.createElement('div');
    grid.className = 'mt-4 grid grid-cols-1 sm:grid-cols-2 gap-3';
    if (mediaItems.length) {
      mediaItems.forEach((item, index) => grid.append(createMediaCard(item.source, item.type, index, entry)));
    } else {
      const empty = document.createElement('p');
      empty.className = 'col-span-full text-xs text-slate-500 border border-dashed border-slate-200 rounded-lg p-6 text-center';
      empty.textContent = '任务完成，但接口没有返回媒体。';
      grid.append(empty);
    }
    article.append(grid);
  }
  return article;
}

function renderHistory() {
  if (!historyEntries.length) {
    const empty = document.createElement('div');
    empty.id = 'history-empty';
    empty.className = 'border border-dashed border-slate-200 rounded-lg min-h-48 flex flex-col items-center justify-center text-slate-400 bg-slate-50/30';
    empty.textContent = '暂无任务记录';
    els['history-list'].replaceChildren(empty);
    return;
  }
  els['history-list'].replaceChildren(...historyEntries.map(createHistoryEntry));
}

function updateGlobalMeta(entry) {
  els['meta-panel'].classList.remove('hidden');
  els['meta-req-id'].textContent = entry.jobId || entry.id;
  els['meta-duration'].textContent = Number.isFinite(entry.durationMs) ? `${entry.durationMs} ms` : '处理中';
  els['meta-wait'].textContent = formatElapsed(entry.elapsedMs || 0);
  els['meta-endpoint'].textContent = entry.endpoint || '等待上游响应';
  els['meta-status'].textContent = statusPresentation(entry.status).text;
}

function createPayload() {
  const config = getProtocolConfig();
  const count = clampInt(els.image_count.value, 1, config.maxOutputs, 1);
  const timeout = clampInt(els.timeout.value, 5, 600, 500);
  const payload = {
    mode: currentMode,
    base_url: els.base_url.value.trim(),
    api_key: els.api_key.value.trim(),
    protocol: config.backendProtocol,
    prompt: els.prompt.value.trim(),
    model: els.model.value,
    size: els.size.value,
    n: count,
    timeout,
  };

  if (config.isVideo) {
    payload.video_resolution = els.video_resolution.value || '720p';
    payload.duration = clampInt(els.duration.value, 1, 15, 10);
    payload.poll_interval = 5;
  }
  if (currentMode === 'edit') {
    payload.edit_images = uploadedReferenceImages.map(({ name, content_type, data_url }) => ({
      name,
      content_type,
      data_url,
    }));
  }
  return payload;
}

function validatePayload(payload) {
  if (!payload.base_url) return '请先填写 Base URL';
  if (!payload.api_key) return '请先填写 API Key';
  if (!payload.prompt) return '请输入提示词';
  if (payload.mode === 'edit' && payload.edit_images.length === 0) return '请先上传至少一张参考图';
  if (payload.mode === 'edit' && payload.edit_images.length > getProtocolConfig().maxReferenceImages) {
    return `当前协议最多上传 ${getProtocolConfig().maxReferenceImages} 张参考图`;
  }
  return '';
}

async function fetchJson(url, options = {}) {
  const response = await fetch(url, { cache: 'no-store', ...options });
  const data = await response.json().catch(() => ({}));
  if (!response.ok || data.ok === false) {
    const error = new Error(formatApiError(data) || `HTTP ${response.status}`);
    error.status = response.status;
    error.payload = data;
    throw error;
  }
  return data;
}

function abortableDelay(milliseconds, signal) {
  return new Promise((resolve, reject) => {
    if (signal.aborted) {
      reject(new DOMException('Aborted', 'AbortError'));
      return;
    }
    const timer = window.setTimeout(resolve, milliseconds);
    signal.addEventListener('abort', () => {
      window.clearTimeout(timer);
      reject(new DOMException('Aborted', 'AbortError'));
    }, { once: true });
  });
}

async function pollJob(task, timeoutSeconds) {
  const deadline = Date.now() + ((timeoutSeconds + 30) * 1000);
  while (Date.now() < deadline) {
    await abortableDelay(POLL_INTERVAL_MS, task.controller.signal);
    const job = await fetchJson(apiPath(`/api/jobs/${encodeURIComponent(task.jobId)}`), {
      signal: task.controller.signal,
    });
    const status = String(job.status || '').toLowerCase();
    if (status === 'success') return job.result || {};
    if (status === 'error' || status === 'failed') {
      throw new Error(formatApiError(job.error || job) || '异步任务执行失败');
    }
    if (status === 'cancelled' || status === 'canceled') {
      throw new DOMException('Cancelled', 'AbortError');
    }
    task.entry.status = status === 'queued' ? 'queued' : 'running';
    updateGlobalMeta(task.entry);
    setStatus(task.entry.status === 'queued' ? '任务已创建，正在排队...' : '任务正在执行，等待媒体结果...');
  }
  throw new Error(`等待任务超过 ${timeoutSeconds + 30} 秒，已停止轮询`);
}

async function requestServerCancellation(jobId) {
  if (!jobId) return false;
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), 3000);
  try {
    const response = await fetch(apiPath(`/api/jobs/${encodeURIComponent(jobId)}`), {
      method: 'DELETE',
      cache: 'no-store',
      signal: controller.signal,
    });
    const data = await response.json().catch(() => ({}));
    const status = String(data.status || '').toLowerCase();
    return response.ok && (data.cancel_requested === true || ['cancelled', 'canceled'].includes(status));
  } catch {
    return false;
  } finally {
    window.clearTimeout(timer);
  }
}

async function cancelActiveTask() {
  const task = activeTask;
  if (!task || task.cancelRequested) return;
  task.cancelRequested = true;
  task.entry.status = 'cancelled';
  task.entry.elapsedMs = Date.now() - task.entry.startedAt;
  task.controller.abort();
  els['cancel-btn'].disabled = true;
  els['cancel-btn'].textContent = '正在取消...';
  const serverCancelled = await requestServerCancellation(task.jobId);
  task.entry.errorMessage = serverCancelled
    ? '任务已在服务端取消。'
    : '已停止本地等待；服务端未确认取消，任务可能仍在后台运行。';
  renderHistory();
  if (activeTask === task || activeTask === null) {
    updateGlobalMeta(task.entry);
    setStatus(task.entry.errorMessage);
  }
}

async function submitGeneration(event) {
  event.preventDefault();
  if (activeTask) return;
  hideError();
  persistSettings();
  const payload = createPayload();
  const validationError = validatePayload(payload);
  if (validationError) {
    showError('无法提交任务', validationError);
    setStatus(validationError);
    return;
  }

  const entry = {
    id: `local-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
    jobId: '',
    status: 'queued',
    startedAt: Date.now(),
    createdAt: Date.now(),
    elapsedMs: 0,
    durationMs: null,
    endpoint: '',
    protocol: getSelectedProtocol(),
    mode: currentMode,
    model: payload.model,
    size: payload.size,
    count: payload.n,
    prompt: payload.prompt,
    images: [],
    videos: [],
    errorMessage: '',
  };
  historyEntries = [entry, ...historyEntries].slice(0, HISTORY_LIMIT);
  const task = { entry, controller: new AbortController(), jobId: '', cancelRequested: false };
  activeTask = task;
  setLoadingState(true);
  els['request-hint'].classList.remove('hidden');
  els['request-message'].textContent = getProtocolConfig().waitingText;
  updateGlobalMeta(entry);
  renderHistory();
  startTaskTimer(entry);
  setStatus('正在创建异步任务...');

  try {
    const created = await fetchJson(apiPath('/api/generate-async'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
      signal: task.controller.signal,
    });
    task.jobId = String(created.job_id || '').trim();
    if (!task.jobId) throw new Error('服务未返回任务 ID');
    entry.jobId = task.jobId;
    entry.status = String(created.status || 'queued').toLowerCase();
    updateGlobalMeta(entry);
    renderHistory();

    const result = await pollJob(task, payload.timeout);
    entry.status = 'success';
    entry.elapsedMs = Date.now() - entry.startedAt;
    entry.durationMs = Number.isFinite(Number(result.duration_ms)) ? Number(result.duration_ms) : entry.elapsedMs;
    entry.endpoint = String(result.endpoint || '');
    entry.images = Array.isArray(result.images) ? result.images : [];
    entry.videos = Array.isArray(result.videos) ? result.videos : [];
    const mediaCount = entry.images.length + entry.videos.length;
    setStatus(mediaCount ? `任务完成，返回 ${mediaCount} 个媒体结果。` : '任务完成，但没有返回媒体。');
  } catch (error) {
    entry.elapsedMs = Date.now() - entry.startedAt;
    if (error?.name === 'AbortError' || task.cancelRequested) {
      entry.status = 'cancelled';
      entry.errorMessage ||= '任务已取消。';
    } else {
      entry.status = 'error';
      entry.errorMessage = error instanceof Error ? error.message : String(error);
      showError('生成任务失败', entry.errorMessage);
      setStatus('任务执行失败。');
    }
  } finally {
    stopTaskTimer();
    els['request-hint'].classList.add('hidden');
    updateGlobalMeta(entry);
    renderHistory();
    if (activeTask === task) activeTask = null;
    setLoadingState(false);
    els['cancel-btn'].disabled = false;
    els['cancel-btn'].textContent = '取消任务';
  }
}

function openLightbox(type, source, title) {
  lastFocusedElement = document.activeElement;
  els['lightbox-title'].textContent = title || '媒体预览';
  if (type === 'video') {
    els['lightbox-image'].removeAttribute('src');
    els['lightbox-image'].classList.add('hidden');
    els['lightbox-video'].src = source;
    els['lightbox-video'].classList.remove('hidden');
  } else {
    els['lightbox-video'].pause();
    els['lightbox-video'].removeAttribute('src');
    els['lightbox-video'].load();
    els['lightbox-video'].classList.add('hidden');
    els['lightbox-image'].src = source;
    els['lightbox-image'].classList.remove('hidden');
  }
  els.lightbox.classList.remove('hidden');
  document.body.classList.add('overflow-hidden');
  els['lightbox-close'].focus();
}

function closeLightbox() {
  if (els.lightbox.classList.contains('hidden')) return;
  els['lightbox-video'].pause();
  els.lightbox.classList.add('hidden');
  document.body.classList.remove('overflow-hidden');
  if (lastFocusedElement instanceof HTMLElement) lastFocusedElement.focus();
}

function hydrateSettings() {
  const settings = loadSettings();
  els.base_url.value = settings.baseUrl;
  els.protocol.value = settings.protocol;
  currentMode = settings.mode;
  els.size.value = settings.size;
  els.image_count.value = String(settings.count);
  els.timeout.value = String(settings.timeout);
  els.video_resolution.value = settings.videoResolution;
  els.duration.value = String(settings.duration);
  applyProtocolPreset(settings.model);
}

function bindEvents() {
  els['protocol-control'].addEventListener('click', (event) => {
    const button = event.target.closest('[data-protocol]');
    if (button) switchProtocol(button.dataset.protocol);
  });
  els['mode-control'].addEventListener('click', (event) => {
    const button = event.target.closest('[data-mode]');
    if (button) switchMode(button.dataset.mode);
  });
  els['config-form'].addEventListener('submit', submitGeneration);
  els['cancel-btn'].addEventListener('click', cancelActiveTask);
  els['clear-history-btn'].addEventListener('click', () => {
    historyEntries = [];
    hideError();
    renderHistory();
    els['meta-panel'].classList.add('hidden');
    setStatus('任务历史已清空。');
  });

  els['reference-dropzone'].addEventListener('click', () => els['file-input'].click());
  els['reference-dropzone'].addEventListener('keydown', (event) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      els['file-input'].click();
    }
  });
  els['file-input'].addEventListener('change', async (event) => {
    hideError();
    try {
      await addReferenceFiles(Array.from(event.target.files || []));
    } catch (error) {
      showError('读取参考图失败', error instanceof Error ? error.message : String(error));
    } finally {
      event.target.value = '';
    }
  });
  els['clear-upload-btn'].addEventListener('click', (event) => {
    event.stopPropagation();
    clearReferenceImages();
  });

  for (const eventName of ['dragenter', 'dragover']) {
    els['reference-dropzone'].addEventListener(eventName, (event) => {
      event.preventDefault();
      els['reference-dropzone'].classList.add('dropzone-active');
    });
  }
  for (const eventName of ['dragleave', 'drop']) {
    els['reference-dropzone'].addEventListener(eventName, (event) => {
      event.preventDefault();
      els['reference-dropzone'].classList.remove('dropzone-active');
    });
  }
  els['reference-dropzone'].addEventListener('drop', async (event) => {
    hideError();
    await addReferenceFiles(Array.from(event.dataTransfer?.files || []));
  });

  for (const id of ['base_url', 'model', 'size', 'image_count', 'timeout', 'video_resolution', 'duration']) {
    els[id].addEventListener('change', persistSettings);
  }
  els['image_count'].addEventListener('blur', () => {
    els['image_count'].value = String(clampInt(els['image_count'].value, 1, getProtocolConfig().maxOutputs, 1));
  });
  els.timeout.addEventListener('blur', () => {
    els.timeout.value = String(clampInt(els.timeout.value, 5, 600, 500));
  });
  els.duration.addEventListener('blur', () => {
    els.duration.value = String(clampInt(els.duration.value, 1, 15, 10));
  });

  els['lightbox-close'].addEventListener('click', closeLightbox);
  els['lightbox-backdrop'].addEventListener('click', closeLightbox);
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') closeLightbox();
  });
  window.addEventListener('online', checkHealth);
  window.addEventListener('offline', () => setHealthStatus(false));
}

hydrateSettings();
bindEvents();
renderHistory();
checkHealth();

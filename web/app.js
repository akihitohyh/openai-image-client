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

const CUSTOM_SIZE_MATRIX = {
  '1:1': ['1024x1024', '2048x2048', '2160x2160'],
  '3:4': ['864x1152', '1728x2304', '2160x2880'],
  '4:3': ['1152x864', '2304x1728', '2880x2160'],
  '16:9': ['1312x736', '2624x1472', '3840x2160'],
  '9:16': ['736x1312', '1472x2624', '2160x3840'],
  '2:3': ['832x1248', '1664x2496', '2160x3240'],
  '3:2': ['1248x832', '2496x1664', '3240x2160'],
  '21:9': ['1568x672', '3136x1344', '3808x1632'],
};
const CUSTOM_RESOLUTION_TIERS = ['1K', '2K', '4K'];

const PROTOCOL_CONFIG = {
  'openai-image': {
    backendProtocol: 'openai',
    defaultModel: 'gpt-image-2',
    models: ['gpt-image-2', 'gpt-image-2.5', 'gpt-image-2.5-flare', 'gpt-image-2.5-sunburst'],
    description: 'OpenAI 生图使用 Images API，支持 gpt-image-2 与 gpt-image-2.5 系列，尺寸按画幅比例和输出档位选择。',
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
    // 模型仍然是下拉选择，只有尺寸改用画幅比例 + 输出档位。
    usesRatioSize: true,
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
  'custom-image': {
    backendProtocol: 'openai',
    defaultModel: '',
    models: [],
    description: '使用 OpenAI 兼容的 Images API，并将自定义模型名称和输出尺寸原样发送给上游。',
    promptPlaceholder: '例如: A professional minimalist workspace, cinematic lighting...',
    promptLabel: '提示词 (Prompt)',
    generateLabel: '文生图 (Generate)',
    editLabel: '图生图 (Edit)',
    modeLabel: '作业模式',
    referenceLabel: '参考图 (OpenAI 兼容接口最多 16 张)',
    uploadPlaceholder: '点击选择或追加参考图像',
    sizeLabel: '分辨率 (Size)',
    countLabel: '生成张数 (n)',
    timeoutLabel: '超时断开 (秒)',
    submitText: '发送生图请求',
    waitingText: '正在等待图片异步任务',
    maxReferenceImages: OPENAI_MAX_REFERENCE_IMAGES,
    maxOutputs: 8,
    isVideo: false,
    usesCustomModel: true,
    usesRatioSize: true,
    modelPlaceholder: '例如: flux-1.1-pro',
  },
  'gemini-image': {
    backendProtocol: 'gemini',
    defaultModel: '',
    models: [],
    description: '使用 Gemini 原生 generateContent 生图协议，模型名称由用户填写。',
    promptPlaceholder: '例如: A cinematic editorial portrait with natural window light...',
    promptLabel: '提示词 (Prompt)',
    generateLabel: '文生图 (Generate)',
    editLabel: '图生图 (Edit)',
    modeLabel: '作业模式',
    referenceLabel: '参考图 (Gemini 最多 16 张)',
    uploadPlaceholder: '点击选择或追加 Gemini 参考图像',
    sizeLabel: '输出尺寸',
    countLabel: '生成张数 (逐次请求)',
    timeoutLabel: '单次超时 (秒)',
    submitText: '发送 Gemini 生图请求',
    waitingText: '正在等待 Gemini 图片异步任务',
    maxReferenceImages: OPENAI_MAX_REFERENCE_IMAGES,
    maxOutputs: 8,
    isVideo: false,
    usesCustomModel: true,
    usesRatioSize: true,
    modelPlaceholder: '例如: gemini-2.5-flash-image',
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
  customModel: '',
  customRatio: '1:1',
  customResolution: '1K',
};

const els = Object.fromEntries([
  'service-status', 'config-form', 'base_url', 'api_key', 'protocol', 'protocol-control',
  'protocol-desc', 'mode-control', 'mode-label', 'mode-generate', 'mode-edit',
  'edit-image-container', 'reference-label', 'reference-dropzone', 'file-input',
  'upload-placeholder', 'upload-preview-grid', 'upload-count', 'clear-upload-btn',
  'prompt-label', 'prompt', 'model', 'size-label', 'size', 'preset-image-options',
  'preset-model-option', 'preset-size-option', 'custom-model-option', 'ratio-size-options',
  'custom-image-options', 'custom_model', 'custom_ratio', 'custom_resolution', 'video-options',
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
let isPreparingSubmission = false;
let taskTimerId = null;
let historyEntries = [];
let lastFocusedElement = null;
// entry.id -> { node, signature }，用于历史列表的增量渲染
const historyNodes = new Map();

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

function getRequestModel() {
  return getProtocolConfig().usesCustomModel ? els.custom_model.value.trim() : els.model.value;
}

function getCustomSize(ratio = els.custom_ratio.value, tier = els.custom_resolution.value) {
  const sizes = CUSTOM_SIZE_MATRIX[ratio] || CUSTOM_SIZE_MATRIX[DEFAULT_SETTINGS.customRatio];
  const tierIndex = CUSTOM_RESOLUTION_TIERS.indexOf(tier);
  return sizes[tierIndex >= 0 ? tierIndex : 0];
}

function getRequestSize() {
  return getProtocolConfig().usesRatioSize ? getCustomSize() : els.size.value;
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

function isKnownSizeOption(value) {
  const options = document.getElementById('size')?.options;
  if (!options || !options.length) return Boolean(value);
  return Array.from(options).some((option) => option.value === String(value));
}

function loadSettings() {
  clearLegacySecrets();
  try {
    const parsed = JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}');

    const protocol = PROTOCOL_CONFIG[parsed.protocol] ? parsed.protocol : DEFAULT_SETTINGS.protocol;
    const config = PROTOCOL_CONFIG[protocol];
    const model = config.models.includes(parsed.model) ? parsed.model : config.defaultModel;
    const customRatio = CUSTOM_SIZE_MATRIX[parsed.customRatio]
      ? parsed.customRatio
      : DEFAULT_SETTINGS.customRatio;
    const storedCustomResolution = parsed.customResolution === '3K' ? '4K' : parsed.customResolution;
    const customResolution = CUSTOM_RESOLUTION_TIERS.includes(storedCustomResolution)
      ? storedCustomResolution
      : DEFAULT_SETTINGS.customResolution;
    return {
      protocol,
      mode: parsed.mode === 'edit' ? 'edit' : 'generate',
      baseUrl: safePersistableBaseUrl(parsed.baseUrl),
      model,
      // 必须对照 <select> 的选项校验：其余字段都验了，只有 size 没验，
      // 于是一个陌生值会让 els.size.value 变成空字符串，最终提交 size: ""。
      size: isKnownSizeOption(parsed.size) ? String(parsed.size) : DEFAULT_SETTINGS.size,
      count: clampInt(parsed.count, 1, config.maxOutputs, DEFAULT_SETTINGS.count),
      timeout: clampInt(parsed.timeout, 5, 600, DEFAULT_SETTINGS.timeout),
      videoResolution: ['480p', '720p', '1080p'].includes(parsed.videoResolution)
        ? parsed.videoResolution
        : DEFAULT_SETTINGS.videoResolution,
      duration: clampInt(parsed.duration, 1, 15, DEFAULT_SETTINGS.duration),
      customModel: typeof parsed.customModel === 'string' ? parsed.customModel.slice(0, 200).trim() : '',
      customRatio,
      customResolution,
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
    customModel: els.custom_model.value.trim(),
    customRatio: els.custom_ratio.value,
    customResolution: els.custom_resolution.value,
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
  // 必须移除初始的 text-zinc-500：它在编译后的 styles.css 里排在状态色之后，
  // 同优先级下会把颜色盖掉（这正是改版前健康灯永远是灰色的原因）。
  els['service-status'].classList.remove('text-zinc-500');
  els['service-status'].classList.toggle('text-emerald-400', ok);
  els['service-status'].classList.toggle('text-rose-400', !ok);
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

// 分段控件的两种状态。写成常量，避免在多处复制同一串 class；
// tailwind.config.js 的 content 包含 app.js，所以这些类名不会被 purge 掉。
const SEGMENT_ACTIVE = 'text-xs py-2 rounded-lg font-medium text-center bg-white/[0.09] text-zinc-100 shadow-sm transition';
const SEGMENT_IDLE = 'text-xs py-2 rounded-lg font-medium text-center text-zinc-500 hover:text-zinc-200 hover:bg-white/[0.04] transition';

function setSegmentedControl(container, attribute, activeValue) {
  for (const button of container.querySelectorAll(`[data-${attribute}]`)) {
    const active = button.dataset[attribute] === activeValue;
    button.setAttribute('aria-pressed', String(active));
    const layoutClass = button.dataset.fullSpan === 'true' ? ' col-span-2' : '';
    button.className = `${active ? SEGMENT_ACTIVE : SEGMENT_IDLE}${layoutClass}`;
  }
}

function updateProtocolButtons() {
  setSegmentedControl(els['protocol-control'], 'protocol', getSelectedProtocol());
}

function updateModeButtons() {
  setSegmentedControl(els['mode-control'], 'mode', currentMode);
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

function updateCustomResolutionOptions() {
  const sizes = CUSTOM_SIZE_MATRIX[els.custom_ratio.value] || CUSTOM_SIZE_MATRIX[DEFAULT_SETTINGS.customRatio];
  Array.from(els.custom_resolution.options).forEach((option, index) => {
    option.textContent = `${option.value} · ${sizes[index]}`;
  });
}

function applyProtocolPreset(preferredModel) {
  const config = getProtocolConfig();
  if (!config.usesCustomModel) updateModelOptions(config, preferredModel);
  updateProtocolButtons();
  updateModeButtons();

  // 模型输入方式与尺寸输入方式是两件独立的事。它们曾经共用 usesCustomModel 这一个
  // 开关，于是「下拉选模型 + 比例档位选尺寸」这种组合根本无法表达。
  const usesCustomModel = Boolean(config.usesCustomModel);
  const usesRatioSize = Boolean(config.usesRatioSize);

  els['preset-model-option'].classList.toggle('hidden', usesCustomModel);
  els['custom-model-option'].classList.toggle('hidden', !usesCustomModel);
  els['preset-size-option'].classList.toggle('hidden', usesRatioSize);
  els['ratio-size-options'].classList.toggle('hidden', !usesRatioSize);
  // 外层容器只在自己两个子项都隐藏时才收起，否则会留下一段空白。
  els['preset-image-options'].classList.toggle('hidden', usesCustomModel && usesRatioSize);
  els['custom-image-options'].classList.toggle('hidden', !usesCustomModel && !usesRatioSize);
  // 模型下拉和尺寸下拉并排才需要两列；只剩一个时让它占满整行。
  els['preset-image-options'].classList.toggle('grid-cols-2', !usesRatioSize);

  els.model.disabled = usesCustomModel;
  els.custom_model.disabled = !usesCustomModel;
  els.custom_model.required = usesCustomModel;
  if (usesCustomModel) {
    els.custom_model.placeholder = config.modelPlaceholder || '请输入模型名称';
  }
  updateCustomResolutionOptions();

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

  // 只保留 File 引用 + 一个 blob: 预览地址。base64 直到提交时才生成，
  // 否则 30MB 上限的参考图会以 ~41MB 的字符串常驻整个会话，并且预览用的
  // <img> 还会再持有一份解码后的副本。
  for (const file of accepted.slice(0, remaining)) {
    uploadedReferenceImages.push({
      name: file.name || 'reference-image',
      content_type: file.type || 'image/png',
      size: file.size,
      file,
      previewUrl: URL.createObjectURL(file),
    });
  }
  renderReferenceImages();
  if (warnings.length) showError('部分参考图未添加', warnings.join('\n'));
}

async function handleIncomingFiles(fileList) {
  hideError();
  try {
    await addReferenceFiles(Array.from(fileList || []));
  } catch (error) {
    showError('读取参考图失败', error instanceof Error ? error.message : String(error));
  }
}

function releaseReferencePreview(reference) {
  if (reference && reference.previewUrl) URL.revokeObjectURL(reference.previewUrl);
}

function removeReferenceImage(index) {
  const [removed] = uploadedReferenceImages.splice(index, 1);
  releaseReferencePreview(removed);
  renderReferenceImages();
}

function clearReferenceImages() {
  uploadedReferenceImages.forEach(releaseReferencePreview);
  uploadedReferenceImages = [];
  els['file-input'].value = '';
  renderReferenceImages();
}

async function buildEditImagePayloads() {
  // 并发读取（原实现在循环里串行 await），并且只在这一刻持有 base64。
  return Promise.all(
    uploadedReferenceImages.map(async ({ name, content_type, file }) => ({
      name,
      content_type,
      data_url: await readFileAsDataUrl(file),
    })),
  );
}

function createReferenceCard(reference, index) {
  const card = document.createElement('div');
  card.className = 'group relative aspect-square overflow-hidden rounded-lg border border-white/10 bg-ink-850 transition hover:border-white/25';
  card.title = `${reference.name} · ${formatBytes(reference.size)}`;

  const preview = document.createElement('button');
  preview.type = 'button';
  preview.className = 'absolute inset-0 z-10 focus:outline-none focus:ring-2 focus:ring-inset focus:ring-accent/60';
  preview.setAttribute('aria-label', `预览参考图 ${index + 1}`);
  preview.addEventListener('click', (event) => {
    event.stopPropagation();
    openLightbox('image', reference.previewUrl, `参考图 #${index + 1}`);
  });

  const image = document.createElement('img');
  image.src = reference.previewUrl;
  image.alt = reference.name || `参考图 ${index + 1}`;
  image.className = 'h-full w-full object-cover';
  image.setAttribute('loading', 'lazy');
  image.setAttribute('decoding', 'async');

  const badge = document.createElement('span');
  badge.className = 'absolute left-1.5 top-1.5 z-20 rounded-md bg-ink-950/80 px-1.5 py-0.5 text-[10px] font-mono text-zinc-300 border border-white/10';
  badge.textContent = `#${index + 1}`;

  const remove = document.createElement('button');
  remove.type = 'button';
  remove.className = 'absolute right-1.5 top-1.5 z-20 rounded-md bg-ink-950/80 px-1.5 py-0.5 text-[10px] text-zinc-300 border border-white/10 opacity-0 group-hover:opacity-100 focus:opacity-100 hover:bg-rose-500 hover:text-white hover:border-rose-400 transition';
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
  // 深色下用「半透明底 + 亮色文字 + 同色描边」，纯色底块会显得脏
  if (status === 'success') return { text: '已完成', classes: 'bg-emerald-500/12 text-emerald-300 border border-emerald-500/25' };
  if (status === 'error') return { text: '失败', classes: 'bg-rose-500/12 text-rose-300 border border-rose-500/25' };
  if (status === 'cancelled') return { text: '已取消', classes: 'bg-white/[0.06] text-zinc-400 border border-white/10' };
  if (status === 'queued') return { text: '排队中', classes: 'bg-amber-500/12 text-amber-300 border border-amber-500/25' };
  return { text: '处理中', classes: 'bg-accent/12 text-accent-soft border border-accent/25' };
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
  const isVideo = type === 'video';
  const label = `${isVideo ? '视频' : '图像'} #${index + 1}`;
  const wrapper = document.createElement('div');
  wrapper.className = isVideo
    ? 'group relative border border-white/10 rounded-xl overflow-hidden bg-ink-950 aspect-video transition hover:border-accent/40'
    : 'group relative border border-white/10 rounded-xl overflow-hidden checkerboard aspect-square transition hover:border-accent/40';

  const media = document.createElement(isVideo ? 'video' : 'img');
  media.src = source;
  media.className = isVideo ? 'w-full h-full object-contain bg-ink-950' : 'w-full h-full object-cover';
  if (isVideo) {
    media.controls = true;
    // preload='metadata' 会让历史里每个视频都发一次 range 请求；改为按需加载。
    media.preload = 'none';
    media.playsInline = true;
  } else {
    media.alt = `生成图像 ${index + 1}`;
    media.referrerPolicy = 'no-referrer';
    media.setAttribute('loading', 'lazy');
    media.setAttribute('decoding', 'async');
  }
  wrapper.append(media);

  const openPreview = () => openLightbox(type, source, label);

  if (isVideo) {
    // 视频不能整卡覆盖：透明层会吃掉原生播放键和进度条。入口放右上角，避开底部控件。
    const expand = document.createElement('button');
    expand.type = 'button';
    expand.className = 'media-corner-action absolute right-2 top-2 z-20 rounded-lg border border-white/15 bg-ink-950/80 px-2 py-1 text-[11px] text-zinc-200 backdrop-blur-sm opacity-0 transition hover:bg-accent hover:text-ink-950 focus:opacity-100 focus:outline-none focus:ring-2 focus:ring-accent/60 group-hover:opacity-100';
    expand.textContent = '放大';
    expand.setAttribute('aria-label', `放大预览${label}`);
    expand.addEventListener('click', openPreview);
    wrapper.append(expand);
  } else {
    // 整张图本身就是打开预览的按钮 —— 参考图卡片一直是这个模式，生成结果之前漏了，
    // 唯一入口是下方那个仅在 hover 时浮现的小字按钮，触屏上几乎够不到。
    const surface = document.createElement('button');
    surface.type = 'button';
    surface.className = 'absolute inset-0 z-10 cursor-zoom-in focus:outline-none focus:ring-2 focus:ring-inset focus:ring-accent/60';
    surface.setAttribute('aria-label', `放大预览${label}`);
    surface.addEventListener('click', openPreview);
    wrapper.append(surface);
  }

  // pointer-events-none 让底部这条渐变不再吃掉点击：图片卡片的点击要落到下面的
  // 整卡按钮上，视频卡片的点击要落到原生控件上。只有下载链接重新打开事件。
  const actions = document.createElement('div');
  actions.className = 'media-actions pointer-events-none absolute inset-x-0 bottom-0 z-20 bg-gradient-to-t from-ink-950/95 to-transparent pt-8 text-zinc-200 text-xs px-3 pb-2.5 flex items-center justify-between gap-2 opacity-0 group-hover:opacity-100 focus-within:opacity-100 transition';

  const caption = document.createElement('span');
  caption.className = 'truncate text-left text-zinc-300';
  caption.textContent = label;

  const download = document.createElement('a');
  download.className = 'pointer-events-auto bg-white/10 hover:bg-accent hover:text-ink-950 text-zinc-100 text-xs px-2.5 py-1 rounded-md border border-white/15 font-medium transition shrink-0 backdrop-blur-sm';
  download.href = source;
  download.download = `${type}_${entry.createdAt}_${index + 1}.${mediaExtension(source, type)}`;
  download.textContent = '下载';

  actions.append(caption, download);
  wrapper.append(actions);
  return wrapper;
}

function createHistoryEntry(entry) {
  const article = document.createElement('article');
  article.dataset.entryId = entry.id;
  article.className = 'border border-white/[0.07] rounded-xl bg-ink-850/40 p-4 animate-fade-in transition hover:border-white/[0.12]';

  const header = document.createElement('div');
  header.className = 'flex items-start justify-between gap-3';
  const summary = document.createElement('div');
  summary.className = 'min-w-0';
  const title = document.createElement('h3');
  title.className = 'text-sm font-semibold text-zinc-100';
  title.textContent = `${PROTOCOL_CONFIG[entry.protocol].isVideo ? '视频' : '图像'}任务 · ${entry.mode === 'edit' ? '参考图模式' : '生成模式'}`;
  const metadata = document.createElement('p');
  metadata.className = 'mt-1 text-[11px] font-mono text-zinc-600 break-all';
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
  prompt.className = 'mt-3 text-xs text-zinc-400 whitespace-pre-wrap break-words leading-relaxed';
  prompt.textContent = entry.prompt;
  article.append(header, prompt);

  if (entry.status === 'queued' || entry.status === 'running') {
    const pending = document.createElement('div');
    pending.className = 'mt-4 border border-dashed border-accent/25 bg-accent/[0.05] rounded-xl min-h-28 flex items-center justify-center text-xs text-accent-soft';
    pending.textContent = '异步任务处理中，结果会自动更新';
    article.append(pending);
  } else if (entry.status === 'error' || entry.status === 'cancelled') {
    const message = document.createElement('pre');
    message.className = entry.status === 'error'
      ? 'mt-4 whitespace-pre-wrap text-xs text-rose-200 bg-rose-500/[0.07] border border-rose-500/20 rounded-lg p-3 leading-relaxed'
      : 'mt-4 whitespace-pre-wrap text-xs text-zinc-400 bg-white/[0.03] border border-white/10 rounded-lg p-3 leading-relaxed';
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
      empty.className = 'col-span-full text-xs text-zinc-500 border border-dashed border-white/10 rounded-xl p-6 text-center';
      empty.textContent = '任务完成，但接口没有返回媒体。';
      grid.append(empty);
    }
    article.append(grid);
  }
  return article;
}

// index.html 里的初始空状态含一个 SVG 图标；克隆它，避免"清空历史"后退化成纯文字。
const emptyHistoryTemplate = document.getElementById('history-empty')?.cloneNode(true) || null;

function buildEmptyHistory() {
  if (emptyHistoryTemplate) return emptyHistoryTemplate.cloneNode(true);
  const empty = document.createElement('div');
  empty.id = 'history-empty';
  empty.className = 'border border-dashed border-white/10 rounded-2xl min-h-[18rem] flex flex-col items-center justify-center text-zinc-500 bg-ink-950/40';
  empty.textContent = '暂无任务记录';
  return empty;
}

// 决定一条历史记录的 DOM 是否需要重建。elapsed 由计时器就地更新（见 startTaskTimer），
// 所以不参与签名，否则每秒都会重建整个列表。
function historyEntrySignature(entry) {
  return [
    entry.status,
    entry.errorMessage || '',
    entry.jobId || '',
    entry.images.join(' '),
    entry.videos.join(' '),
  ].join('|');
}

function renderHistory() {
  if (!historyEntries.length) {
    historyNodes.clear();
    els['history-list'].replaceChildren(buildEmptyHistory());
    return;
  }

  // 复用未变化的节点：replaceChildren 移动既有节点而不是重建，
  // 因此 <img>/<video> 不会重新发起请求，也不会闪烁。
  const nodes = historyEntries.map((entry) => {
    const signature = historyEntrySignature(entry);
    const cached = historyNodes.get(entry.id);
    if (cached && cached.signature === signature) return cached.node;
    const node = createHistoryEntry(entry);
    historyNodes.set(entry.id, { node, signature });
    return node;
  });

  const liveIds = new Set(historyEntries.map((entry) => entry.id));
  for (const id of Array.from(historyNodes.keys())) {
    if (!liveIds.has(id)) historyNodes.delete(id);
  }

  els['history-list'].replaceChildren(...nodes);
}

function updateGlobalMeta(entry) {
  els['meta-panel'].classList.remove('hidden');
  els['meta-req-id'].textContent = entry.jobId || entry.id;
  els['meta-duration'].textContent = Number.isFinite(entry.durationMs) ? `${entry.durationMs} ms` : '处理中';
  els['meta-wait'].textContent = formatElapsed(entry.elapsedMs || 0);
  els['meta-endpoint'].textContent = entry.endpoint || '等待上游响应';
  els['meta-status'].textContent = statusPresentation(entry.status).text;
}

async function createPayload() {
  const config = getProtocolConfig();
  const count = clampInt(els.image_count.value, 1, config.maxOutputs, 1);
  const timeout = clampInt(els.timeout.value, 5, 600, 500);
  const payload = {
    mode: currentMode,
    base_url: els.base_url.value.trim(),
    api_key: els.api_key.value.trim(),
    protocol: config.backendProtocol,
    prompt: els.prompt.value.trim(),
    model: getRequestModel(),
    size: getRequestSize(),
    n: count,
    timeout,
  };

  if (config.backendProtocol === 'gemini') {
    payload.aspect_ratio = els.custom_ratio.value;
    payload.resolution = els.custom_resolution.value;
  }
  if (config.isVideo) {
    payload.video_resolution = els.video_resolution.value || '720p';
    payload.duration = clampInt(els.duration.value, 1, 15, 10);
    payload.poll_interval = 5;
  }
  if (currentMode === 'edit') {
    payload.edit_images = await buildEditImagePayloads();
  }
  return payload;
}

function validatePayload(payload) {
  if (!payload.base_url) return '请先填写 Base URL';
  if (!payload.api_key) return '请先填写 API Key';
  if (!payload.prompt) return '请输入提示词';
  if (!payload.model) return '请输入模型名称';
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
    // { once: true } 只在 abort 真的触发时才移除监听器。正常超时返回的路径上
    // 监听器会留在 signal 上，长任务轮询几百次就会堆积几百个闭包，必须手动摘除。
    const onAbort = () => {
      window.clearTimeout(timer);
      reject(new DOMException('Aborted', 'AbortError'));
    };
    const timer = window.setTimeout(() => {
      signal.removeEventListener('abort', onAbort);
      resolve();
    }, milliseconds);
    signal.addEventListener('abort', onAbort, { once: true });
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
  // 本地放弃等待时必须通知服务端，否则任务会继续占用 worker 槽位、
  // 并把没人会读取的资产一直保留到 TTL 过期。
  const cancelled = await requestServerCancellation(task.jobId);
  throw new Error(
    `等待任务超过 ${timeoutSeconds + 30} 秒，已停止轮询${cancelled ? '，服务端任务已取消' : '，服务端未确认取消'}`,
  );
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
  els['cancel-btn'].disabled = true;
  els['cancel-btn'].textContent = '正在取消...';
  // 先挂上取消 promise 再 abort：abort() 会同步 reject 轮询，于是 submitGeneration
  // 的 finally 会在下一个微任务就把按钮恢复成"取消任务"，把上面这行刚设置的
  // "正在取消..."立刻抹掉——而服务端取消请求还有最多 3 秒才回来。
  // finally 需要能看到这个 promise 并等它结束。
  task.cancelPromise = requestServerCancellation(task.jobId).then((serverCancelled) => {
    task.entry.errorMessage = serverCancelled
      ? '任务已在服务端取消。'
      : '已停止本地等待；服务端未确认取消，任务可能仍在后台运行。';
    return serverCancelled;
  });
  task.controller.abort();
  await task.cancelPromise;
  renderHistory();
  if (activeTask === task || activeTask === null) {
    updateGlobalMeta(task.entry);
    setStatus(task.entry.errorMessage);
  }
}

async function submitGeneration(event) {
  event.preventDefault();
  // createPayload 现在要读取参考图文件（异步），在 activeTask 被赋值之前存在一个窗口，
  // 期间连点提交会并发起两个任务，因此需要额外的提交中标记。
  if (activeTask || isPreparingSubmission) return;
  hideError();
  persistSettings();

  isPreparingSubmission = true;
  let payload;
  try {
    payload = await createPayload();
  } catch (error) {
    showError('无法提交任务', error instanceof Error ? error.message : '读取参考图失败');
    setStatus('读取参考图失败，请重新选择');
    return;
  } finally {
    isPreparingSubmission = false;
  }
  if (activeTask) return;

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
  const task = { entry, controller: new AbortController(), jobId: '', cancelRequested: false, cancelPromise: null };
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
    // 等服务端取消请求真正结束，再恢复按钮，否则"正在取消..."会被立刻覆盖。
    // requestServerCancellation 自身不会 reject，catch 只是兜底。
    if (task.cancelPromise) {
      try {
        await task.cancelPromise;
      } catch {
        // 取消结果已写入 entry.errorMessage，这里无需额外处理。
      }
    }
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
  // 只 pause 不清 src：远程 mp4 关闭后仍会继续缓冲，data URL 预览也会一直保持解码状态。
  els['lightbox-video'].pause();
  els['lightbox-video'].removeAttribute('src');
  els['lightbox-video'].load();
  els['lightbox-image'].removeAttribute('src');
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
  els.custom_model.value = settings.customModel;
  els.custom_ratio.value = settings.customRatio;
  els.custom_resolution.value = settings.customResolution;
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
    try {
      await handleIncomingFiles(event.target.files);
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
  els['reference-dropzone'].addEventListener('drop', (event) => {
    // 走同一个 helper：原来的 drop 分支没有 try/catch，
    // FileReader 失败会变成未处理的 promise rejection，用户什么也看不到。
    void handleIncomingFiles(event.dataTransfer?.files);
  });

  for (const id of [
    'base_url', 'model', 'size', 'custom_model', 'custom_resolution',
    'image_count', 'timeout', 'video_resolution', 'duration',
  ]) {
    els[id].addEventListener('change', persistSettings);
  }
  els.custom_ratio.addEventListener('change', () => {
    updateCustomResolutionOptions();
    persistSettings();
  });
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

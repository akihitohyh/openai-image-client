const STORAGE_KEY = 'openai-image-client-web:v3';

const defaults = {
  baseUrl: '',
  apiKey: '',
  model: 'gpt-image-2',
  count: 1,
  timeout: 120,
  prompt: '',
  imageMode: 'generate',
  sizeMode: 'preset',
  presetBase: '1K',
  presetRatio: '1:1',
  customWidth: 1024,
  customHeight: 1024,
  theme: 'light',
};

const examplePrompt = 'A cinematic futuristic city at sunset, wet streets, neon reflections, volumetric light, ultra detailed.';
const baseMap = { '1K': 1024, '2K': 2048 };
const ratioMap = {
  '1:1': [1, 1],
  '3:2': [3, 2],
  '2:3': [2, 3],
  '16:9': [16, 9],
  '9:16': [9, 16],
  '4:3': [4, 3],
  '3:4': [3, 4],
  '21:9': [21, 9],
};
const restrictedRatios = new Set(['16:9', '9:16']);

let previewImages = [];
let previewIndex = 0;
let historyEntries = [];
let selectedHistoryId = null;
let activePendingId = null;
let activeTimerId = null;
let editImageState = null;
let dragDepth = 0;

const els = {
  baseUrl: document.getElementById('baseUrl'),
  apiKey: document.getElementById('apiKey'),
  model: document.getElementById('model'),
  count: document.getElementById('count'),
  timeout: document.getElementById('timeout'),
  prompt: document.getElementById('prompt'),
  generateBtn: document.getElementById('generateBtn'),
  fillExampleBtn: document.getElementById('fillExampleBtn'),
  clearPromptBtn: document.getElementById('clearPromptBtn'),
  resetSettingsBtn: document.getElementById('resetSettingsBtn'),
  clearHistoryBtn: document.getElementById('clearHistoryBtn'),
  gallery: document.getElementById('gallery'),
  errorBox: document.getElementById('errorBox'),
  statusBadge: document.getElementById('statusBadge'),
  metaInfo: document.getElementById('metaInfo'),
  saveState: document.getElementById('saveState'),
  imageCardTemplate: document.getElementById('imageCardTemplate'),
  historyBoardTemplate: document.getElementById('historyBoardTemplate'),
  statModel: document.getElementById('statModel'),
  statSize: document.getElementById('statSize'),
  statCount: document.getElementById('statCount'),
  summaryBaseUrl: document.getElementById('summaryBaseUrl'),
  summarySize: document.getElementById('summarySize'),
  summaryTimeout: document.getElementById('summaryTimeout'),
  summaryTheme: document.getElementById('summaryTheme'),
  promptDesc: document.getElementById('promptDesc'),
  modeBadge: document.getElementById('modeBadge'),
  themeToggle: document.getElementById('themeToggle'),
  imageModeGenerate: document.getElementById('imageModeGenerate'),
  imageModeEdit: document.getElementById('imageModeEdit'),
  editImagePanel: document.getElementById('editImagePanel'),
  editImageInput: document.getElementById('editImageInput'),
  editImageDropzone: document.getElementById('editImageDropzone'),
  editImagePreviewCard: document.getElementById('editImagePreviewCard'),
  editImagePreview: document.getElementById('editImagePreview'),
  editImageMeta: document.getElementById('editImageMeta'),
  clearEditImageBtn: document.getElementById('clearEditImageBtn'),
  sizeModePreset: document.getElementById('sizeModePreset'),
  sizeModeCustom: document.getElementById('sizeModeCustom'),
  presetSizePanel: document.getElementById('presetSizePanel'),
  customSizePanel: document.getElementById('customSizePanel'),
  presetBase: document.getElementById('presetBase'),
  presetRatio: document.getElementById('presetRatio'),
  customWidth: document.getElementById('customWidth'),
  customHeight: document.getElementById('customHeight'),
  resolvedSizeBadge: document.getElementById('resolvedSizeBadge'),
  lightbox: document.getElementById('lightbox'),
  lightboxBackdrop: document.getElementById('lightboxBackdrop'),
  lightboxImage: document.getElementById('lightboxImage'),
  lightboxCounter: document.getElementById('lightboxCounter'),
  lightboxPrev: document.getElementById('lightboxPrev'),
  lightboxNext: document.getElementById('lightboxNext'),
  lightboxClose: document.getElementById('lightboxClose'),
};

function clampInt(value, min, max) {
  const num = Number(value);
  if (!Number.isFinite(num)) return min;
  return Math.min(max, Math.max(min, Math.round(num)));
}

function snap16(value) {
  const num = Number(value);
  if (!Number.isFinite(num) || num <= 0) return 16;
  return Math.max(16, Math.round(num / 16) * 16);
}

function migrateLegacyState(raw) {
  const next = { ...defaults, ...(raw || {}) };
  if (raw && raw.size && !raw.sizeMode) {
    const match = String(raw.size).match(/^(\d+)x(\d+)$/i);
    if (match) {
      next.sizeMode = 'custom';
      next.customWidth = Number(match[1]);
      next.customHeight = Number(match[2]);
    }
  }
  next.count = clampInt(next.count, 1, 8);
  return next;
}

function loadState() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return { ...defaults };
    return migrateLegacyState(JSON.parse(raw));
  } catch {
    return { ...defaults };
  }
}

function truncateMiddle(value, max = 34) {
  if (!value) return 'Base URL 未填写';
  if (value.length <= max) return value;
  const left = Math.ceil((max - 1) / 2);
  const right = Math.floor((max - 1) / 2);
  return `${value.slice(0, left)}…${value.slice(-right)}`;
}

function formatBytes(bytes) {
  const size = Number(bytes);
  if (!Number.isFinite(size) || size <= 0) return '0 B';
  if (size < 1024) return `${size} B`;
  if (size < 1024 ** 2) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 ** 2)).toFixed(1)} MB`;
}

function formatClock(ms) {
  const totalSeconds = Math.max(0, Math.floor(ms / 1000));
  const minutes = String(Math.floor(totalSeconds / 60)).padStart(2, '0');
  const seconds = String(totalSeconds % 60).padStart(2, '0');
  return `${minutes}:${seconds}`;
}

function formatDateTime(timestamp) {
  return new Date(timestamp).toLocaleString('zh-CN', {
    hour12: false,
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });
}

function applyTheme(theme) {
  const nextTheme = theme === 'dark' ? 'dark' : 'light';
  document.documentElement.dataset.theme = nextTheme;
  els.themeToggle.classList.toggle('active', nextTheme === 'dark');
  els.themeToggle.setAttribute('aria-pressed', String(nextTheme === 'dark'));
  els.summaryTheme.textContent = nextTheme === 'dark' ? 'DARK 模式' : 'LIGHT 模式';
}

function getModeLabel(mode) {
  return mode === 'edit' ? '图生图' : '文生图';
}

function updatePromptPlaceholder(mode) {
  els.prompt.placeholder = mode === 'edit'
    ? '请输入你希望在参考图基础上生成的效果，例如：保留主体姿态，改成赛博朋克夜景风格...'
    : '请输入生图提示词...';
  els.promptDesc.textContent = mode === 'edit'
    ? '上传一张参考图，再输入提示词进行图生图。错误信息会在当前页面显示。'
    : '输入提示词后直接生成图片，错误信息会在当前页面显示。';
  els.generateBtn.textContent = mode === 'edit' ? '开始图生图' : '生成图片';
  els.modeBadge.textContent = getModeLabel(mode);
}

function getPresetSize(baseKey, ratioKey) {
  const longEdge = baseMap[baseKey] || 1024;
  const ratio = ratioMap[ratioKey] || [1, 1];
  const [rw, rh] = ratio;
  if (rw >= rh) {
    const width = longEdge;
    const height = snap16((longEdge * rh) / rw);
    return `${width}x${height}`;
  }
  const height = longEdge;
  const width = snap16((longEdge * rw) / rh);
  return `${width}x${height}`;
}

function getEffectiveSize(state) {
  if (state.sizeMode === 'custom') {
    const width = Number(state.customWidth);
    const height = Number(state.customHeight);
    if (!Number.isFinite(width) || !Number.isFinite(height) || width < 16 || height < 16) {
      return { error: '自定义宽高必须是大于等于 16 的数字' };
    }
    if (width % 16 !== 0 || height % 16 !== 0) {
      return { error: '自定义宽高只能填写 16 的倍数' };
    }
    return { value: `${width}x${height}` };
  }
  return { value: getPresetSize(state.presetBase, state.presetRatio) };
}

function syncPresetRatioAvailability() {
  const base = els.presetBase.value;
  const currentRatio = els.presetRatio.value;
  Array.from(els.presetRatio.options).forEach((option) => {
    const restricted = restrictedRatios.has(option.value);
    const enabled = !restricted || base === '2K';
    option.disabled = !enabled;
    option.hidden = false;
  });
  if (restrictedRatios.has(currentRatio) && base !== '2K') {
    els.presetRatio.value = '1:1';
  }
}

function updateSizeModeUI(mode) {
  const preset = mode !== 'custom';
  els.sizeModePreset.classList.toggle('active', preset);
  els.sizeModeCustom.classList.toggle('active', !preset);
  els.presetSizePanel.classList.toggle('hidden', !preset);
  els.customSizePanel.classList.toggle('hidden', preset);
}

function updateImageModeUI(mode) {
  const isEdit = mode === 'edit';
  els.imageModeGenerate.classList.toggle('active', !isEdit);
  els.imageModeEdit.classList.toggle('active', isEdit);
  els.editImagePanel.classList.toggle('hidden', !isEdit);
  updatePromptPlaceholder(mode);
  renderEditImagePreview();
}

function currentState() {
  return {
    baseUrl: els.baseUrl.value.trim(),
    apiKey: els.apiKey.value.trim(),
    model: els.model.value.trim(),
    count: clampInt(els.count.value || 1, 1, 8),
    timeout: clampInt(els.timeout.value || 120, 10, 600),
    prompt: els.prompt.value,
    imageMode: els.imageModeEdit.classList.contains('active') ? 'edit' : 'generate',
    sizeMode: els.sizeModeCustom.classList.contains('active') ? 'custom' : 'preset',
    presetBase: els.presetBase.value,
    presetRatio: els.presetRatio.value,
    customWidth: Number(els.customWidth.value || 0),
    customHeight: Number(els.customHeight.value || 0),
    theme: document.documentElement.dataset.theme === 'dark' ? 'dark' : 'light',
  };
}

function updateSummary(state) {
  const size = getEffectiveSize(state);
  const sizeText = size.value || size.error || '尺寸未设置';
  els.statModel.textContent = state.model || defaults.model;
  els.statSize.textContent = sizeText;
  els.statCount.textContent = String(clampInt(state.count || defaults.count, 1, 8));
  els.summaryBaseUrl.textContent = truncateMiddle(state.baseUrl);
  els.summarySize.textContent = `尺寸 ${sizeText}`;
  els.summaryTimeout.textContent = `超时 ${clampInt(state.timeout || defaults.timeout, 10, 600)} 秒`;
  els.resolvedSizeBadge.textContent = sizeText;
  applyTheme(state.theme);
}

function saveState(next) {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
  els.saveState.textContent = `设置已自动保存到浏览器本地 · ${new Date().toLocaleTimeString()}`;
  updateSummary(next);
}

function hydrateForm(state) {
  els.baseUrl.value = state.baseUrl || '';
  els.apiKey.value = state.apiKey || '';
  els.model.value = state.model || defaults.model;
  els.count.value = clampInt(state.count || defaults.count, 1, 8);
  els.timeout.value = clampInt(state.timeout || defaults.timeout, 10, 600);
  els.prompt.value = state.prompt || '';
  updateImageModeUI(state.imageMode || defaults.imageMode);
  els.presetBase.value = state.presetBase || defaults.presetBase;
  syncPresetRatioAvailability();
  els.presetRatio.value = state.presetRatio || defaults.presetRatio;
  syncPresetRatioAvailability();
  els.customWidth.value = Number(state.customWidth || defaults.customWidth);
  els.customHeight.value = Number(state.customHeight || defaults.customHeight);
  updateSizeModeUI(state.sizeMode || defaults.sizeMode);
  updateSummary({ ...defaults, ...state });
}

function normalizeCustomInputs() {
  if (els.customWidth.value) els.customWidth.value = String(snap16(els.customWidth.value));
  if (els.customHeight.value) els.customHeight.value = String(snap16(els.customHeight.value));
  saveState(currentState());
}

function normalizeCountInput() {
  els.count.value = String(clampInt(els.count.value || 1, 1, 8));
  saveState(currentState());
}

function normalizeTimeoutInput() {
  els.timeout.value = String(clampInt(els.timeout.value || 120, 10, 600));
  saveState(currentState());
}

function renderEditImagePreview() {
  const active = currentState().imageMode === 'edit';
  els.editImagePreviewCard.classList.toggle('hidden', !editImageState || !active);

  if (!editImageState) {
    els.editImageDropzone.classList.remove('is-filled');
    els.editImageMeta.textContent = '尚未选择图片';
    els.editImagePreview.removeAttribute('src');
    return;
  }

  els.editImageDropzone.classList.add('is-filled');
  els.editImagePreview.src = editImageState.dataUrl;
  els.editImagePreview.alt = editImageState.name || 'reference image preview';
  const parts = [
    editImageState.name || 'reference-image',
    editImageState.contentType || 'image/png',
  ];
  if (Number.isFinite(editImageState.size)) parts.push(formatBytes(editImageState.size));
  els.editImageMeta.textContent = parts.join(' · ');
}

function setEditImageState(nextImage) {
  editImageState = nextImage;
  renderEditImagePreview();
}

function clearEditImageState() {
  setEditImageState(null);
  els.editImageInput.value = '';
}

function readFileAsDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ''));
    reader.onerror = () => reject(new Error('读取图片失败，请重新选择文件'));
    reader.readAsDataURL(file);
  });
}

async function handleEditImageFile(file) {
  if (!file) return;
  const allowedTypes = new Set(['image/png', 'image/jpeg', 'image/webp']);
  if (!allowedTypes.has(file.type)) {
    throw new Error('图生图目前仅支持 PNG / JPG / WEBP');
  }
  if (file.size > 50 * 1024 * 1024) {
    throw new Error('参考图不能超过 50 MB');
  }

  const dataUrl = await readFileAsDataUrl(file);
  setEditImageState({
    name: file.name || 'reference-image',
    contentType: file.type || 'image/png',
    size: file.size,
    dataUrl,
  });
}

function bindAutosave() {
  const watchList = [
    els.baseUrl,
    els.apiKey,
    els.model,
    els.count,
    els.timeout,
    els.prompt,
    els.presetBase,
    els.presetRatio,
    els.customWidth,
    els.customHeight,
  ];

  ['input', 'change'].forEach((eventName) => {
    watchList.forEach((el) => {
      el.addEventListener(eventName, () => saveState(currentState()));
    });
  });

  [els.customWidth, els.customHeight].forEach((el) => {
    el.addEventListener('blur', normalizeCustomInputs);
  });
  els.count.addEventListener('blur', normalizeCountInput);
  els.timeout.addEventListener('blur', normalizeTimeoutInput);
  els.presetBase.addEventListener('change', () => {
    syncPresetRatioAvailability();
    saveState(currentState());
  });
}

function setStatus(kind, text) {
  const classMap = {
    idle: 'status-queued',
    loading: 'status-running',
    success: 'status-completed',
    warning: 'status-warning',
    error: 'status-danger',
  };
  els.statusBadge.className = `status-badge ${classMap[kind] || 'status-queued'}`;
  els.statusBadge.textContent = text;
}

function updateLightboxControls() {
  const total = previewImages.length;
  const hasMultiple = total > 1;
  els.lightboxCounter.textContent = total ? `${previewIndex + 1} / ${total}` : '0 / 0';
  els.lightboxPrev.disabled = !hasMultiple;
  els.lightboxNext.disabled = !hasMultiple;
}

function renderLightbox() {
  if (!previewImages.length) return;
  els.lightboxImage.src = previewImages[previewIndex];
  els.lightboxImage.alt = `preview image ${previewIndex + 1}`;
  updateLightboxControls();
}

function openLightbox(images, index) {
  if (!images.length) return;
  previewImages = [...images];
  previewIndex = index;
  renderLightbox();
  els.lightbox.classList.remove('hidden');
  document.body.style.overflow = 'hidden';
}

function closeLightbox() {
  els.lightbox.classList.add('hidden');
  document.body.style.overflow = '';
}

function moveLightbox(step) {
  if (previewImages.length <= 1) return;
  previewIndex = (previewIndex + step + previewImages.length) % previewImages.length;
  renderLightbox();
}

function showError(message) {
  els.errorBox.textContent = message;
  els.errorBox.classList.remove('hidden');
}

function clearError() {
  els.errorBox.textContent = '';
  els.errorBox.classList.add('hidden');
}

function renderEmptyHistory(message = '填写连接设置与提示词后，点击“生成图片”。') {
  els.gallery.innerHTML = `
    <div class="empty-state">
      <div class="empty-state-badge">READY</div>
      <div class="empty-title">还没有生成结果</div>
      <div class="empty-desc">${message}</div>
    </div>
  `;
  els.gallery.classList.add('empty');
}

function getPendingMeta(entry) {
  return `模式：${getModeLabel(entry.mode)} · 模型：${entry.model} · 尺寸：${entry.size} · 当前任务：${entry.sequence}/${entry.total} · 已等待 ${formatClock(entry.elapsedMs || 0)}`;
}

function getSuccessMeta(entry) {
  const parts = [
    `模式：${getModeLabel(entry.mode)}`,
    `完成时间：${formatDateTime(entry.createdAt)}`,
    `模型：${entry.model}`,
    `尺寸：${entry.size}`,
    `任务：${entry.sequence}/${entry.total}`,
  ];
  if (Number.isFinite(entry.durationMs)) {
    parts.push(`耗时：${formatClock(entry.durationMs)}`);
  }
  if (entry.endpoint) {
    parts.push(`接口：${entry.endpoint}`);
  }
  return parts.join(' · ');
}

function getErrorMeta(entry) {
  const parts = [
    `模式：${getModeLabel(entry.mode)}`,
    `失败时间：${formatDateTime(entry.createdAt)}`,
    `模型：${entry.model}`,
    `尺寸：${entry.size}`,
    `任务：${entry.sequence}/${entry.total}`,
  ];
  if (Number.isFinite(entry.durationMs)) {
    parts.push(`等待：${formatClock(entry.durationMs)}`);
  }
  return parts.join(' · ');
}

function getEntryMeta(entry) {
  if (entry.status === 'pending') return getPendingMeta(entry);
  if (entry.status === 'success') return getSuccessMeta(entry);
  return getErrorMeta(entry);
}

function getEntryStatusConfig(entry) {
  if (entry.status === 'pending') {
    return {
      badgeClass: 'status-running',
      badgeText: `等待 ${formatClock(entry.elapsedMs || 0)}`,
      chipClass: 'image-chip-running',
      chipText: '生成中',
    };
  }
  if (entry.status === 'success') {
    return {
      badgeClass: 'status-completed',
      badgeText: '已完成',
      chipClass: 'image-chip-success',
      chipText: '已完成',
    };
  }
  return {
    badgeClass: 'status-danger',
    badgeText: '失败',
    chipClass: 'image-chip-error',
    chipText: '失败',
  };
}

function getEntryPlaceholderText(entry) {
  if (entry.status === 'pending') return '生成中';
  if (entry.status === 'error') return '生成失败';
  return '没有返回图片';
}

function findEntryOrderById(id) {
  const index = historyEntries.findIndex((entry) => entry.id === id);
  return index >= 0 ? index + 1 : 0;
}

function ensureSelectedHistoryId() {
  if (selectedHistoryId && historyEntries.some((entry) => entry.id === selectedHistoryId)) return;
  selectedHistoryId = null;
}

function getPreviewGalleryItems() {
  return historyEntries.flatMap((entry) => (
    Array.isArray(entry.images)
      ? entry.images.map((src, imageIndex) => ({ entryId: entry.id, src, imageIndex }))
      : []
  ));
}

function openEntryPreview(entry) {
  if (!Array.isArray(entry.images) || !entry.images.length) return;
  const previewItems = getPreviewGalleryItems();
  const previewIndex = previewItems.findIndex((item) => item.entryId === entry.id);
  if (previewIndex >= 0) {
    openLightbox(previewItems.map((item) => item.src), previewIndex);
    return;
  }
  openLightbox(entry.images, 0);
}

function createImageCard(entry, order) {
  const node = els.imageCardTemplate.content.firstElementChild.cloneNode(true);
  const img = node.querySelector('img');
  const indexNode = node.querySelector('.image-index');
  const chip = node.querySelector('.image-chip');
  const placeholder = node.querySelector('.image-placeholder');
  const status = getEntryStatusConfig(entry);

  node.dataset.entryId = entry.id;
  node.classList.toggle('is-selected', entry.id === selectedHistoryId);
  node.addEventListener('click', () => {
    selectedHistoryId = entry.id;
    renderHistory();
  });

  indexNode.textContent = `#${order}`;
  chip.textContent = status.chipText;
  chip.className = `image-chip ${status.chipClass}`;

  if (Array.isArray(entry.images) && entry.images[0]) {
    img.src = entry.images[0];
    img.alt = `generated image ${order}`;
    placeholder.classList.add('hidden');
  } else {
    img.removeAttribute('src');
    img.alt = getEntryPlaceholderText(entry);
    img.classList.add('hidden');
    placeholder.classList.remove('hidden');
    placeholder.textContent = getEntryPlaceholderText(entry);
    placeholder.classList.toggle('is-error', entry.status === 'error');
  }

  return node;
}

function createDetailLabel(text) {
  const label = document.createElement('div');
  label.className = 'history-detail-label';
  label.textContent = text;
  return label;
}

function renderHistoryDetail(container, entry, order) {
  container.innerHTML = '';

  if (!entry) {
    const empty = document.createElement('div');
    empty.className = 'history-detail-empty';
    empty.textContent = '点击左侧图片后，这里会显示详细信息和提示词。';
    container.appendChild(empty);
    return;
  }

  const status = getEntryStatusConfig(entry);
  const card = document.createElement('article');
  card.className = 'history-detail-card';

  const preview = document.createElement(Array.isArray(entry.images) && entry.images[0] ? 'button' : 'div');
  preview.className = 'history-detail-preview';
  if (preview instanceof HTMLButtonElement) {
    preview.type = 'button';
    preview.addEventListener('click', () => openEntryPreview(entry));
  }

  if (Array.isArray(entry.images) && entry.images[0]) {
    const img = document.createElement('img');
    img.className = 'history-detail-image';
    img.src = entry.images[0];
    img.alt = `generated image ${order}`;
    preview.appendChild(img);
  } else {
    const placeholder = document.createElement('div');
    placeholder.className = 'history-detail-placeholder';
    if (entry.status === 'error') placeholder.classList.add('is-error');
    placeholder.textContent = getEntryPlaceholderText(entry);
    preview.appendChild(placeholder);
  }

  const tagRow = document.createElement('div');
  tagRow.className = 'history-detail-tags';

  const orderTag = document.createElement('div');
  orderTag.className = 'mini-tag mini-tag-blue';
  orderTag.textContent = `序号 #${order}`;
  tagRow.appendChild(orderTag);

  const modeTag = document.createElement('div');
  modeTag.className = 'mini-tag mini-tag-white';
  modeTag.textContent = getModeLabel(entry.mode);
  tagRow.appendChild(modeTag);

  const statusBadge = document.createElement('div');
  statusBadge.className = `status-badge ${status.badgeClass}`;
  statusBadge.textContent = status.badgeText;
  tagRow.appendChild(statusBadge);

  const metaBlock = document.createElement('div');
  metaBlock.className = 'history-detail-meta';
  metaBlock.appendChild(createDetailLabel('详细信息'));
  const metaContent = document.createElement('div');
  metaContent.className = 'history-detail-text';
  metaContent.textContent = getEntryMeta(entry);
  metaBlock.appendChild(metaContent);

  const promptBlock = document.createElement('div');
  promptBlock.className = 'history-detail-prompt';
  promptBlock.appendChild(createDetailLabel('提示词'));
  const promptContent = document.createElement('div');
  promptContent.className = 'history-detail-text';
  promptContent.textContent = entry.prompt || '未填写提示词';
  promptBlock.appendChild(promptContent);

  let referenceBlock = null;
  if (entry.sourceImageDataUrl) {
    referenceBlock = document.createElement('div');
    referenceBlock.className = 'history-detail-reference';
    referenceBlock.appendChild(createDetailLabel('参考图'));

    const referencePreview = document.createElement('button');
    referencePreview.className = 'history-detail-reference-preview';
    referencePreview.type = 'button';
    referencePreview.addEventListener('click', () => openLightbox([entry.sourceImageDataUrl], 0));

    const referenceImage = document.createElement('img');
    referenceImage.className = 'history-detail-reference-image';
    referenceImage.src = entry.sourceImageDataUrl;
    referenceImage.alt = entry.sourceImageName || 'reference image';
    referencePreview.appendChild(referenceImage);
    referenceBlock.appendChild(referencePreview);

    const referenceMeta = document.createElement('div');
    referenceMeta.className = 'history-detail-text';
    referenceMeta.textContent = entry.sourceImageName
      ? `参考图：${entry.sourceImageName}`
      : '已上传参考图';
    referenceBlock.appendChild(referenceMeta);
  }

  const actions = document.createElement('div');
  actions.className = 'history-detail-actions';

  if (Array.isArray(entry.images) && entry.images[0]) {
    const previewBtn = document.createElement('button');
    previewBtn.className = 'btn btn-white neo-interactive btn-sm';
    previewBtn.type = 'button';
    previewBtn.textContent = '放大预览';
    previewBtn.addEventListener('click', () => openEntryPreview(entry));
    actions.appendChild(previewBtn);

    const downloadLink = document.createElement('a');
    downloadLink.className = 'btn btn-green neo-interactive btn-sm download-link';
    downloadLink.href = entry.images[0];
    downloadLink.download = `generated-${entry.createdAt || Date.now()}-${order}.png`;
    downloadLink.textContent = '下载图片';
    actions.appendChild(downloadLink);
  }

  if (entry.status === 'error' && entry.errorMessage) {
    const errorBlock = document.createElement('div');
    errorBlock.className = 'history-detail-error';
    errorBlock.appendChild(createDetailLabel('错误信息'));
    const errorText = document.createElement('div');
    errorText.className = 'history-detail-text';
    errorText.textContent = entry.errorMessage;
    errorBlock.appendChild(errorText);
    card.appendChild(preview);
    card.appendChild(tagRow);
    card.appendChild(metaBlock);
    card.appendChild(promptBlock);
    if (referenceBlock) card.appendChild(referenceBlock);
    card.appendChild(errorBlock);
    if (actions.childNodes.length) card.appendChild(actions);
    container.appendChild(card);
    return;
  }

  card.appendChild(preview);
  card.appendChild(tagRow);
  card.appendChild(metaBlock);
  card.appendChild(promptBlock);
  if (referenceBlock) card.appendChild(referenceBlock);
  if (actions.childNodes.length) card.appendChild(actions);
  container.appendChild(card);
}

function renderHistory() {
  if (!historyEntries.length) {
    selectedHistoryId = null;
    renderEmptyHistory();
    return;
  }

  ensureSelectedHistoryId();
  els.gallery.innerHTML = '';
  els.gallery.classList.remove('empty');

  const board = els.historyBoardTemplate.content.firstElementChild.cloneNode(true);
  const list = board.querySelector('.history-board-list');
  const detail = board.querySelector('.history-detail');
  const count = board.querySelector('.history-board-count');
  count.textContent = `${historyEntries.length} 个结果`;

  historyEntries.forEach((entry, index) => {
    list.appendChild(createImageCard(entry, index + 1));
  });

  const selectedEntry = selectedHistoryId
    ? historyEntries.find((entry) => entry.id === selectedHistoryId) || null
    : null;
  renderHistoryDetail(detail, selectedEntry, selectedEntry ? findEntryOrderById(selectedEntry.id) : 0);

  els.gallery.appendChild(board);
}

function stopRequestTimer() {
  if (activeTimerId) {
    clearInterval(activeTimerId);
    activeTimerId = null;
  }
  activePendingId = null;
}

function findEntryById(id) {
  return historyEntries.find((entry) => entry.id === id) || null;
}

function updatePendingEntryUI(entry) {
  if (selectedHistoryId !== entry.id) return;
  const detail = els.gallery.querySelector('.history-detail');
  if (!detail) return;
  renderHistoryDetail(detail, entry, findEntryOrderById(entry.id));
}

function startRequestTimer(entry) {
  stopRequestTimer();
  activePendingId = entry.id;

  const tick = () => {
    const activeEntry = findEntryById(activePendingId);
    if (!activeEntry) {
      stopRequestTimer();
      return;
    }
    activeEntry.elapsedMs = Date.now() - activeEntry.startedAt;
    const taskLabel = activeEntry.total > 1
      ? `串行 ${activeEntry.sequence}/${activeEntry.total}`
      : '生成中';
    setStatus('loading', `${taskLabel} ${formatClock(activeEntry.elapsedMs)}`);
    els.metaInfo.textContent = `当前任务 ${activeEntry.sequence}/${activeEntry.total} · 已等待 ${formatClock(activeEntry.elapsedMs)}`;
    updatePendingEntryUI(activeEntry);
  };

  tick();
  activeTimerId = setInterval(tick, 1000);
}

function formatError(data) {
  const parts = [];
  if (data.message) parts.push(`错误：${data.message}`);
  if (data.last_endpoint) parts.push(`最后请求地址：${data.last_endpoint}`);
  if (data.client_request_id) parts.push(`客户端请求 ID：${data.client_request_id}`);
  if (Array.isArray(data.diagnostics) && data.diagnostics.length) {
    parts.push('尝试过的地址：');
    parts.push(...data.diagnostics);
  }
  return parts.join('\n');
}

async function runSingleGenerateTask(state, sizeValue, sequence, total) {
  const entry = {
    id: `job-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
    status: 'pending',
    mode: state.imageMode || 'generate',
    prompt: state.prompt.trim(),
    model: state.model || defaults.model,
    size: sizeValue,
    total,
    sequence,
    createdAt: Date.now(),
    startedAt: Date.now(),
    elapsedMs: 0,
    images: [],
    errorMessage: '',
    sourceImageDataUrl: state.imageMode === 'edit' ? (editImageState?.dataUrl || '') : '',
    sourceImageName: state.imageMode === 'edit' ? (editImageState?.name || '') : '',
  };

  historyEntries = [...historyEntries, entry];
  renderHistory();
  startRequestTimer(entry);

  try {
    const requestPayload = {
      base_url: state.baseUrl,
      api_key: state.apiKey,
      prompt: state.prompt,
      model: state.model,
      size: sizeValue,
      n: 1,
      timeout: state.timeout,
      mode: state.imageMode || 'generate',
    };
    if (requestPayload.mode === 'edit' && editImageState) {
      requestPayload.edit_image = {
        name: editImageState.name,
        content_type: editImageState.contentType,
        data_url: editImageState.dataUrl,
      };
    }

    const response = await fetch('/api/generate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(requestPayload),
    });

    const data = await response.json().catch(() => ({}));
    if (!response.ok || !data.ok) {
      throw new Error(formatError(data));
    }

    entry.elapsedMs = Date.now() - entry.startedAt;
    stopRequestTimer();
    entry.status = 'success';
    entry.images = Array.isArray(data.images) ? data.images : [];
    entry.durationMs = typeof data.duration_ms === 'number' ? data.duration_ms : entry.elapsedMs;
    entry.endpoint = data.endpoint || '';
    entry.clientRequestId = data.client_request_id || '';
    renderHistory();
    return { ok: true, entry, data };
  } catch (error) {
    entry.elapsedMs = Date.now() - entry.startedAt;
    stopRequestTimer();
    entry.status = 'error';
    entry.durationMs = entry.elapsedMs;
    entry.errorMessage = error instanceof Error ? error.message : String(error);
    renderHistory();
    return { ok: false, entry, error: entry.errorMessage };
  }
}

async function generate() {
  clearError();
  const state = currentState();
  const size = getEffectiveSize(state);
  saveState(state);

  if (!state.baseUrl) {
    setStatus('warning', '缺少 Base URL');
    return showError('请先填写 Base URL');
  }
  if (!state.apiKey) {
    setStatus('warning', '缺少 API Key');
    return showError('请先填写 API Key');
  }
  if (!state.prompt.trim()) {
    setStatus('warning', '缺少提示词');
    return showError('请输入提示词');
  }
  if (state.imageMode === 'edit' && !editImageState) {
    setStatus('warning', '缺少参考图');
    return showError('图生图模式下请先上传一张参考图');
  }
  if (size.error) {
    setStatus('warning', '尺寸配置错误');
    return showError(size.error);
  }

  els.generateBtn.disabled = true;

  const total = clampInt(state.count, 1, 8);
  let successCount = 0;
  let failureCount = 0;
  const errorMessages = [];

  for (let sequence = 1; sequence <= total; sequence += 1) {
    const result = await runSingleGenerateTask(state, size.value, sequence, total);
    if (result.ok) {
      successCount += 1;
    } else {
      failureCount += 1;
      errorMessages.push(`任务 ${sequence}/${total}\n${result.error}`);
    }
  }

  if (failureCount === 0) {
    setStatus('success', total > 1 ? `全部完成 ${successCount}/${total}` : '生成完成');
    els.metaInfo.textContent = total > 1
      ? `串行任务全部完成 · 成功 ${successCount}/${total}`
      : '生成完成';
  } else if (successCount > 0) {
    setStatus('warning', `完成 ${successCount}/${total}`);
    els.metaInfo.textContent = `部分完成 · 成功 ${successCount}/${total} · 失败 ${failureCount}`;
    showError(errorMessages.join('\n\n'));
  } else {
    setStatus('error', '全部失败');
    els.metaInfo.textContent = `全部失败 · 共 ${failureCount} 个任务`;
    showError(errorMessages.join('\n\n'));
  }

  els.generateBtn.disabled = false;
}

function resetSettings() {
  localStorage.removeItem(STORAGE_KEY);
  clearEditImageState();
  hydrateForm(defaults);
  clearError();
  els.metaInfo.textContent = '本地设置已清空，请重新填写连接信息';
  setStatus('idle', '待命');
  els.saveState.textContent = '设置已清空，请重新填写';
}

function clearHistory() {
  historyEntries = historyEntries.filter((entry) => entry.status === 'pending');
  ensureSelectedHistoryId();
  renderHistory();
  els.metaInfo.textContent = historyEntries.length ? '历史已清空，当前仍有任务生成中' : '历史已清空';
}

function init() {
  const state = loadState();
  hydrateForm(state);
  bindAutosave();
  renderEditImagePreview();

  els.generateBtn.addEventListener('click', generate);
  els.fillExampleBtn.addEventListener('click', () => {
    els.prompt.value = examplePrompt;
    saveState(currentState());
  });
  els.clearPromptBtn.addEventListener('click', () => {
    els.prompt.value = '';
    saveState(currentState());
  });
  els.resetSettingsBtn.addEventListener('click', resetSettings);
  els.clearHistoryBtn.addEventListener('click', clearHistory);

  els.themeToggle.addEventListener('click', () => {
    const state = currentState();
    state.theme = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    saveState(state);
  });

  els.imageModeGenerate.addEventListener('click', () => {
    updateImageModeUI('generate');
    saveState(currentState());
  });
  els.imageModeEdit.addEventListener('click', () => {
    updateImageModeUI('edit');
    saveState(currentState());
  });

  els.editImageDropzone.addEventListener('click', () => els.editImageInput.click());
  els.editImageDropzone.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      els.editImageInput.click();
    }
  });
  els.editImageInput.addEventListener('change', async () => {
    const [file] = Array.from(els.editImageInput.files || []);
    if (!file) return;
    try {
      await handleEditImageFile(file);
      clearError();
    } catch (error) {
      showError(error instanceof Error ? error.message : String(error));
    } finally {
      els.editImageInput.value = '';
    }
  });
  els.clearEditImageBtn.addEventListener('click', () => clearEditImageState());

  els.editImageDropzone.addEventListener('dragenter', (event) => {
    event.preventDefault();
    dragDepth += 1;
    els.editImageDropzone.classList.add('is-dragover');
  });
  els.editImageDropzone.addEventListener('dragover', (event) => {
    event.preventDefault();
    els.editImageDropzone.classList.add('is-dragover');
  });
  ['dragleave', 'dragend'].forEach((eventName) => {
    els.editImageDropzone.addEventListener(eventName, (event) => {
      event.preventDefault();
      dragDepth = Math.max(0, dragDepth - 1);
      if (dragDepth === 0) {
        els.editImageDropzone.classList.remove('is-dragover');
      }
    });
  });
  els.editImageDropzone.addEventListener('drop', async (event) => {
    event.preventDefault();
    dragDepth = 0;
    els.editImageDropzone.classList.remove('is-dragover');
    const [file] = Array.from(event.dataTransfer?.files || []);
    if (!file) return;
    try {
      await handleEditImageFile(file);
      clearError();
    } catch (error) {
      showError(error instanceof Error ? error.message : String(error));
    }
  });

  els.sizeModePreset.addEventListener('click', () => {
    updateSizeModeUI('preset');
    saveState(currentState());
  });
  els.sizeModeCustom.addEventListener('click', () => {
    updateSizeModeUI('custom');
    saveState(currentState());
  });

  els.lightboxBackdrop.addEventListener('click', closeLightbox);
  els.lightboxClose.addEventListener('click', closeLightbox);
  els.lightboxPrev.addEventListener('click', () => moveLightbox(-1));
  els.lightboxNext.addEventListener('click', () => moveLightbox(1));
  document.addEventListener('keydown', (event) => {
    if (els.lightbox.classList.contains('hidden')) return;
    if (event.key === 'Escape') closeLightbox();
    if (event.key === 'ArrowLeft') moveLightbox(-1);
    if (event.key === 'ArrowRight') moveLightbox(1);
  });

  setStatus('idle', '待命');
  els.metaInfo.textContent = '尚未发起请求';
  renderHistory();
}

init();

import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import vm from 'node:vm';

const indexHtml = await readFile(new URL('./index.html', import.meta.url), 'utf8');
const appJs = await readFile(new URL('./app.js', import.meta.url), 'utf8');
const stylesCss = await readFile(new URL('./styles.css', import.meta.url), 'utf8');

test('page uses only local external assets and no inline event handlers', () => {
  assert.doesNotMatch(indexHtml, /<script(?![^>]+src=)[^>]*>/i);
  assert.doesNotMatch(indexHtml, /<script[^>]+src=["']https?:/i);
  assert.doesNotMatch(indexHtml, /\son(?:click|change|submit|load)=/i);
  assert.match(indexHtml, /<link[^>]+href=["']\.\/styles\.css["']/i);
  assert.match(indexHtml, /<link[^>]+href=["']\.\/favicon\.svg["']/i);
  assert.match(indexHtml, /<script[^>]+src=["']\.\/app\.js["']/i);
});

test('frontend uses the asynchronous job API', () => {
  assert.match(appJs, /api\/generate-async/);
  assert.match(appJs, /api\/jobs\/\$\{encodeURIComponent\(task\.jobId\)\}/);
  assert.doesNotMatch(appJs, /api\/generate["'`]/);
});

test('cancel button uses the backend DELETE job contract', () => {
  const cancelBody = appJs.match(/async function requestServerCancellation\(jobId\) \{([\s\S]*?)\n\}/)?.[1] || '';
  assert.ok(cancelBody, 'requestServerCancellation function must exist');
  assert.match(cancelBody, /api\/jobs\/\$\{encodeURIComponent\(jobId\)\}/);
  assert.match(cancelBody, /method: 'DELETE'/);
  assert.doesNotMatch(cancelBody, /\/cancel/);
  assert.match(appJs, /activeTask === task \|\| activeTask === null/);
});

test('download names preserve local asset media extensions', () => {
  assert.match(appJs, /new URL\(String\(source\), window\.location\.href\)\.pathname/);
  assert.match(appJs, /\['png', 'jpg', 'jpeg', 'webp', 'gif'\]\.includes\(extension\)/);
  assert.match(appJs, /\['mp4', 'webm'\]\.includes\(extension\)/);
});

test('persisted settings never include secrets or generated content', () => {
  const persistBody = appJs.match(/function persistSettings\(\) \{([\s\S]*?)\n\}/)?.[1] || '';
  assert.ok(persistBody, 'persistSettings function must exist');
  assert.doesNotMatch(persistBody, /api_?key/i);
  assert.doesNotMatch(persistBody, /prompt|edit_images|data_url/i);
  assert.match(appJs, /localStorage\.removeItem\(key\)/);
});

test('storage failures do not break iframe initialization or submission', () => {
  assert.match(appJs, /function clearLegacySecrets\(\) \{[\s\S]*?try \{[\s\S]*?localStorage\.removeItem/);
  assert.match(appJs, /function loadSettings\(\) \{[\s\S]*?try \{[\s\S]*?localStorage\.getItem/);
  assert.match(appJs, /function persistSettings\(\) \{[\s\S]*?try \{[\s\S]*?localStorage\.setItem/);
});

test('compiled stylesheet exists and Tailwind CDN is absent', () => {
  assert.ok(stylesCss.length > 1000, 'compiled CSS should not be empty');
  assert.doesNotMatch(indexHtml, /cdn\.tailwindcss\.com/i);
});

test('frontend limits match backend request constraints', () => {
  assert.match(appJs, /MAX_REFERENCE_IMAGE_BYTES = 10 \* 1024 \* 1024/);
  assert.match(appJs, /MAX_REFERENCE_IMAGES_TOTAL_BYTES = 30 \* 1024 \* 1024/);
  assert.match(appJs, /'grok-video':[\s\S]*?maxOutputs: 4/);
  assert.match(appJs, /timeout: clampInt\(parsed\.timeout, 5, 600,/);
  assert.match(indexHtml, /id="timeout"[^>]+max="600"/);
});

test('mobile layout keeps the result panel reachable', () => {
  assert.match(indexHtml, /overflow-y-visible md:overflow-hidden/);
  assert.match(indexHtml, /overflow-y-visible md:overflow-y-auto/);
  assert.match(indexHtml, /min-h-\[520px\] md:min-h-0/);
});

test('custom OpenAI-compatible model UI is available', () => {
  assert.match(indexHtml, /data-protocol="custom-image"[^>]*>其他模型</);
  assert.match(indexHtml, /id="custom_model"[^>]+maxlength="200"/);
  assert.match(indexHtml, /id="custom_ratio"/);
  assert.match(indexHtml, /id="custom_resolution"/);
  assert.match(appJs, /'custom-image': \{[\s\S]*?backendProtocol: 'openai'/);
  assert.match(appJs, /if \(!payload\.model\) return '请输入模型名称'/);
});

test('OpenAI image protocol offers gpt-image-2.5 variants and 4K sizes', () => {
  // 模型下拉包含 gpt-image-2.5 系列（app.js 的 PROTOCOL_CONFIG 是渲染数据源）。
  assert.match(appJs, /'openai-image': \{[\s\S]*?models: \['gpt-image-2', 'gpt-image-2\.5-flare', 'gpt-image-2\.5-sunburst'\]/);
  // index.html 静态 option 是无 JS 回退，须与 PROTOCOL_CONFIG 保持一致。
  assert.match(indexHtml, /<option value="gpt-image-2\.5-flare">/);
  assert.match(indexHtml, /<option value="gpt-image-2\.5-sunburst">/);
  // 4K 档沿用 CUSTOM_SIZE_MATRIX 的 4K 语义（16:9/9:16 → 3840x2160/2160x3840）。
  assert.match(indexHtml, /<option value="3840x2160">/);
  assert.match(indexHtml, /<option value="2160x3840">/);
});

test('Gemini native image protocol reuses manual model and UHD controls', () => {
  assert.match(indexHtml, /data-protocol="gemini-image"[^>]*>Gemini 生图</);
  assert.match(appJs, /'gemini-image': \{[\s\S]*?backendProtocol: 'gemini'/);
  assert.match(appJs, /'gemini-image': \{[\s\S]*?usesCustomModel: true/);
  assert.match(appJs, /payload\.aspect_ratio = els\.custom_ratio\.value/);
  assert.match(appJs, /payload\.resolution = els\.custom_resolution\.value/);
  assert.match(appJs, /countLabel: '生成张数 \(逐次请求\)'/);
  assert.match(indexHtml, /data-protocol="custom-image"[^>]+data-full-span="true"/);
  assert.match(appJs, /button\.dataset\.fullSpan === 'true'/);
});

test('custom ratio and resolution tiers map to the required output sizes', () => {
  const source = appJs.match(
    /const CUSTOM_SIZE_MATRIX = ([\s\S]*?);\nconst CUSTOM_RESOLUTION_TIERS/,
  )?.[1];
  assert.ok(source, 'CUSTOM_SIZE_MATRIX must exist');
  const matrix = vm.runInNewContext(`(${source})`);
  assert.deepEqual(JSON.parse(JSON.stringify(matrix)), {
    '1:1': ['1024x1024', '2048x2048', '2160x2160'],
    '3:4': ['864x1152', '1728x2304', '2160x2880'],
    '4:3': ['1152x864', '2304x1728', '2880x2160'],
    '16:9': ['1312x736', '2624x1472', '3840x2160'],
    '9:16': ['736x1312', '1472x2624', '2160x3840'],
    '2:3': ['832x1248', '1664x2496', '2160x3240'],
    '3:2': ['1248x832', '2496x1664', '3240x2160'],
    '21:9': ['1568x672', '3136x1344', '3808x1632'],
  });
  assert.match(appJs, /const CUSTOM_RESOLUTION_TIERS = \['1K', '2K', '4K'\]/);
  assert.match(appJs, /parsed\.customResolution === '3K' \? '4K' : parsed\.customResolution/);
  assert.match(appJs, /size: getRequestSize\(\)/);

  for (const [ratio, sizes] of Object.entries(matrix)) {
    const [ratioWidth, ratioHeight] = ratio.split(':').map(Number);
    for (const size of sizes) {
      const [width, height] = size.split('x').map(Number);
      if (ratioWidth >= ratioHeight) {
        assert.ok(width <= 3840 && height <= 2160, `${ratio} ${size} exceeds the UHD limit`);
      } else {
        assert.ok(width <= 2160 && height <= 3840, `${ratio} ${size} exceeds the portrait UHD limit`);
      }
    }
  }
});

test('custom model settings are persisted without changing existing model settings', () => {
  const persistBody = appJs.match(/function persistSettings\(\) \{([\s\S]*?)\n\}/)?.[1] || '';
  assert.match(persistBody, /customModel: els\.custom_model\.value\.trim\(\)/);
  assert.match(persistBody, /customRatio: els\.custom_ratio\.value/);
  assert.match(persistBody, /customResolution: els\.custom_resolution\.value/);
  assert.match(
    appJs,
    /getProtocolConfig\(\)\.usesCustomModel \? els\.custom_model\.value\.trim\(\) : els\.model\.value/,
  );
});

test('generated images open the lightbox when the image itself is clicked', () => {
  const body = appJs.match(/function createMediaCard\(([\s\S]*?)\n\}/)?.[1] || '';
  assert.ok(body, 'createMediaCard must exist');
  // 只看 openPreview 之后的分支，并按 else 切开，分别验证两侧
  const branch = body.split('const openPreview')[1] || '';
  const [videoBranch, imageBranch] = branch.split('} else {');
  assert.ok(videoBranch && imageBranch, 'preview entry must branch on media type');

  // 图片分支：整卡透明按钮 + 放大光标，和参考图卡片同一个模式
  assert.match(imageBranch, /surface\.className = '[^']*absolute inset-0[^']*cursor-zoom-in/);
  assert.match(imageBranch, /surface\.addEventListener\('click', openPreview\)/);

  // 视频分支：只能是角标入口。整卡覆盖会吃掉原生播放键和进度条。
  assert.match(videoBranch, /expand\.className = '[^']*absolute right-2 top-2/);
  assert.match(videoBranch, /expand\.addEventListener\('click', openPreview\)/);
  assert.doesNotMatch(videoBranch, /absolute inset-0/);
});

test('the bottom action bar does not swallow clicks meant for the media', () => {
  const body = appJs.match(/function createMediaCard\(([\s\S]*?)\n\}/)?.[1] || '';
  // 渐变条覆盖在图片/视频控件之上，必须放行点击，只有下载链接重新接收事件
  assert.match(body, /actions\.className = '[^']*pointer-events-none/);
  assert.match(body, /download\.className = '[^']*pointer-events-auto/);
});

test('hover-only affordances stay reachable on touch devices', () => {
  // 触屏没有 hover，group-hover 浮现的入口在手机上等于不存在
  assert.match(stylesCss, /@media\s*\(hover:\s*none\)/);
  assert.match(stylesCss, /\.media-actions[^{]*\{[^}]*opacity:\s*1/);
  assert.match(stylesCss, /\.media-corner-action/);
  // 覆盖规则必须排在 opacity-0 之后，否则同优先级下被盖掉
  assert.ok(
    stylesCss.indexOf('.media-actions') > stylesCss.lastIndexOf('.opacity-0{'),
    'touch override must come after .opacity-0 in the compiled sheet',
  );
});

test('compiled stylesheet carries the new preview utilities', () => {
  for (const utility of ['cursor-zoom-in', 'pointer-events-none', 'pointer-events-auto']) {
    assert.ok(stylesCss.includes(utility), `${utility} must be compiled into styles.css`);
  }
});

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
  assert.match(appJs, /MAX_REFERENCE_IMAGE_BYTES = 20 \* 1024 \* 1024/);
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

test('OpenAI protocol offers the gpt-image-2 and gpt-image-2.5 families', () => {
  const source = appJs.match(/'openai-image': \{([\s\S]*?)\n  \},/)?.[1] || '';
  assert.ok(source, "the 'openai-image' protocol config must exist");
  // vm 里求值出来的数组来自另一个 realm，原型不同，必须先过一次 JSON 才能 deepEqual
  const models = JSON.parse(
    JSON.stringify(vm.runInNewContext(`(${source.match(/models: (\[[^\]]*\])/)?.[1]})`)),
  );
  assert.deepEqual(models, [
    'gpt-image-2',
    'gpt-image-2.5',
    'gpt-image-2.5-flare',
    'gpt-image-2.5-sunburst',
  ]);
  // 默认模型必须在列表里，否则 loadSettings 会回落到一个下拉中不存在的值
  const defaultModel = source.match(/defaultModel: '([^']*)'/)?.[1];
  assert.ok(models.includes(defaultModel), `defaultModel ${defaultModel} must be selectable`);
});

test('every protocol preset keeps its default model selectable', () => {
  // updateModelOptions 只渲染 config.models，defaultModel 不在其中就会选出空值
  const configs = appJs.match(/'[a-z-]+': \{[\s\S]*?\n  \},/g) || [];
  assert.ok(configs.length >= 5, 'expected all protocol presets to be found');
  for (const config of configs) {
    const name = config.match(/^'([a-z-]+)'/)?.[1];
    const raw = config.match(/models: (\[[^\]]*\])/)?.[1];
    if (!raw) continue;
    const models = vm.runInNewContext(`(${raw})`);
    const defaultModel = config.match(/defaultModel: '([^']*)'/)?.[1] ?? '';
    if (models.length === 0) continue; // 自定义模型协议由用户手填
    assert.ok(models.includes(defaultModel), `${name}: defaultModel ${defaultModel} not in models`);
  }
});

test('model input and size input are independently switchable', () => {
  // 两者曾共用 usesCustomModel，导致「下拉选模型 + 比例档位选尺寸」无法表达
  const block = appJs.match(/const PROTOCOL_CONFIG = \{([\s\S]*?)\n\};/)?.[1] || '';
  const actual = {};
  for (const [, name, body] of block.matchAll(/'([a-z-]+)': \{([\s\S]*?)\n  \},/g)) {
    actual[name] = {
      customModel: /usesCustomModel: true/.test(body),
      ratioSize: /usesRatioSize: true/.test(body),
    };
  }
  assert.deepEqual(actual, {
    'openai-image': { customModel: false, ratioSize: true },
    'grok-image': { customModel: false, ratioSize: false },
    'grok-video': { customModel: false, ratioSize: false },
    'custom-image': { customModel: true, ratioSize: true },
    'gemini-image': { customModel: true, ratioSize: true },
  });
  // 尺寸来源看 usesRatioSize，模型来源看 usesCustomModel，不能再混用
  assert.match(appJs, /usesRatioSize \? getCustomSize\(\) : els\.size\.value/);
  assert.match(appJs, /usesCustomModel \? els\.custom_model\.value\.trim\(\) : els\.model\.value/);
});

test('markup exposes the four independently toggled option blocks', () => {
  for (const id of ['preset-model-option', 'preset-size-option', 'custom-model-option', 'ratio-size-options']) {
    assert.match(indexHtml, new RegExp(`id="${id}"`), `#${id} must exist in the markup`);
    assert.match(appJs, new RegExp(`'${id}'`), `#${id} must be wired in app.js`);
  }
});

test('generated images can be reused as edit references', () => {
  const body = appJs.match(/async function reuseGeneratedImage\(([\s\S]*?)\n\}/)?.[1] || '';
  assert.ok(body, 'reuseGeneratedImage must exist');
  // 必须复用手动上传的入口，否则类型/大小/张数四层校验会被绕过
  assert.match(body, /await addReferenceFiles\(\[file\]\)/);
  // 任务运行中不能加参考图：switchMode 此时是 no-op
  assert.match(body, /if \(activeTask\)/);
  // 资产 1 小时过期，404 要给出可操作的说明而不是裸 HTTP 码
  assert.match(body, /response\.status === 404/);
  assert.match(body, /已过期/);
  // 成功后切到图生图，否则参考图加了却看不见
  assert.match(body, /switchMode\('edit'\)/);
  // 校验没通过时不能谎报成功
  assert.match(body, /uploadedReferenceImages\.length === before/);
});

test('the reuse button is image-only and re-enables pointer events', () => {
  const body = appJs.match(/function createMediaCard\(([\s\S]*?)\n\}/)?.[1] || '';
  // `if (!isVideo) {` 之后的那一段就是图片专属分支
  const imageOnlyBlock = (body.split('if (!isVideo) {')[1] || '').split('\n  }')[0];
  assert.ok(imageOnlyBlock, 'the reuse button must sit behind an !isVideo guard');
  assert.match(imageOnlyBlock, /reuse\.addEventListener\('click'/);
  // 底部渐变条是 pointer-events-none 的，按钮必须自己开回来才能点
  assert.match(imageOnlyBlock, /reuse\.className = '[^']*pointer-events-auto/);
  // 视频卡片不能出现复用按钮
  const videoOnlyBlock = (body.split('if (isVideo) {')[1] || '').split('\n  } else {')[0];
  assert.doesNotMatch(videoOnlyBlock, /reuse/);
  // 标题在 flex 里要 min-w-0 才会截断，否则会把按钮挤出卡片
  assert.match(body, /caption\.className = '[^']*min-w-0[^']*truncate/);
});

test('reference size limits stay within the backend request body cap', () => {
  const single = Number(appJs.match(/MAX_REFERENCE_IMAGE_BYTES = (\d+) \* 1024 \* 1024/)?.[1]);
  const total = Number(appJs.match(/MAX_REFERENCE_IMAGES_TOTAL_BYTES = (\d+) \* 1024 \* 1024/)?.[1]);
  assert.ok(single <= total, 'a single reference must fit within the total budget');
  // 后端 MAX_REQUEST_BODY_BYTES 是 48MB，base64 会把原始字节放大 4/3
  assert.ok((total * 4) / 3 < 48, `total ${total}MB becomes ${(total * 4) / 3}MB after base64, over the 48MB body cap`);
  // 警告文案必须从常量推导，不能再写死数字
  assert.match(appJs, /单张不能超过 \$\{formatBytes\(MAX_REFERENCE_IMAGE_BYTES\)\}/);
});

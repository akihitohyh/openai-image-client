import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

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

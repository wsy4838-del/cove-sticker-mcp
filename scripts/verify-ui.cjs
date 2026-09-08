#!/usr/bin/env node

const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

const root = path.resolve(__dirname, '..');
const index = path.join(root, 'src', 'sticker_mcp', 'static', 'index.html');
const onePixel = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=',
  'base64',
);

const fixture = {
  id: 'one',
  filename: '<script>alert(1)</script>.png',
  mime_type: 'image/png',
  extension: '.png',
  byte_size: onePixel.length,
  width: 1,
  height: 1,
  description: '<b>未标注的表情</b>',
  ocr_text: '嘿 <img src=x>',
  semantic_description: '一个测试表情',
  emotions: ['开心'],
  scenes: ['日常'],
  keywords: ['测试'],
  deleted: false,
  last_feedback: null,
  manually_edited: false,
  use_count: 0,
};

function json(res, value, status = 200) {
  res.writeHead(status, { 'content-type': 'application/json; charset=utf-8' });
  res.end(JSON.stringify(value));
}

function readRequestBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    req.on('data', (chunk) => chunks.push(chunk));
    req.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')));
    req.on('error', reject);
  });
}

async function startServer() {
  const requests = [];
  const state = {
    item: structuredClone(fixture),
    settings: {
      enabled: true,
      assistant_enabled: true,
      auto_tag: true,
      show_feedback: false,
      casual_frequency: 'normal',
      work_frequency: 'rare',
      avoid_recent: 3,
    },
    vision: {
      provider: 'openai-compatible',
      model: 'vision-test',
      base_url: 'https://example.test/v1',
      api_key: '••••1234',
      configured: true,
    },
    jobs: [{ id: 7, sticker_id: 'one', status: 'failed', error: 'mock failed' }],
  };
  const server = http.createServer(async (req, res) => {
    const url = new URL(req.url, 'http://127.0.0.1');
    const requestRecord = { method: req.method, path: url.pathname, query: url.search, headers: req.headers };
    requests.push(requestRecord);
    if (url.pathname === '/' && req.method === 'GET') {
      res.writeHead(200, { 'content-type': 'text/html; charset=utf-8' });
      return res.end(fs.readFileSync(index));
    }
    if (url.pathname === '/api/csrf' && req.method === 'GET') {
      if (req.headers.authorization !== 'Bearer demo-token') return json(res, { error: 'forbidden' }, 403);
      return json(res, { token: 'csrf-test-token' });
    }
    if (url.pathname === '/assets/one' && req.method === 'GET') {
      res.writeHead(200, { 'content-type': 'image/png' });
      return res.end(onePixel);
    }
    if (url.pathname === '/api/stickers' && req.method === 'GET') {
      const includeDeleted = url.searchParams.get('include_deleted') === '1';
      const q = (url.searchParams.get('q') || '').toLowerCase();
      const matches = !q || JSON.stringify(state.item).toLowerCase().includes(q);
      return json(res, {
        items: matches && (includeDeleted || !state.item.deleted) ? [state.item] : [],
        total: matches && (includeDeleted || !state.item.deleted) ? 1 : 0,
        page: Number(url.searchParams.get('page') || 1),
        page_size: Number(url.searchParams.get('page_size') || 24),
      });
    }
    if (url.pathname === '/api/stickers' && req.method === 'POST') {
      await readRequestBody(req);
      return json(res, { imported: [state.item], duplicates: [], rejected: [{ filename: 'bad.gif', error: 'mock rejected' }], job_ids: [8] }, 201);
    }
    if (url.pathname === '/api/stickers/one' && req.method === 'GET') return json(res, state.item);
    if (url.pathname === '/api/stickers/one' && req.method === 'PATCH') {
      const raw = await readRequestBody(req);
      requestRecord.body = raw;
      const payload = JSON.parse(raw);
      Object.assign(state.item, payload, { manually_edited: true });
      return json(res, state.item);
    }
    if (url.pathname === '/api/stickers/one' && req.method === 'DELETE') {
      state.item.deleted = true;
      return json(res, { ok: true });
    }
    if (url.pathname === '/api/stickers/one/restore' && req.method === 'POST') {
      state.item.deleted = false;
      return json(res, state.item);
    }
    if (url.pathname === '/api/stickers/one/feedback' && req.method === 'POST') {
      const payload = JSON.parse(await readRequestBody(req));
      state.item.last_feedback = payload.value === 'clear' ? null : payload.value;
      return json(res, state.item);
    }
    if (url.pathname === '/api/settings' && req.method === 'GET') return json(res, state.settings);
    if (url.pathname === '/api/settings' && req.method === 'PATCH') {
      const raw = await readRequestBody(req);
      requestRecord.body = raw;
      Object.assign(state.settings, JSON.parse(raw));
      return json(res, state.settings);
    }
    if (url.pathname === '/api/vision' && req.method === 'GET') return json(res, state.vision);
    if (url.pathname === '/api/vision' && req.method === 'PATCH') {
      const raw = await readRequestBody(req);
      requestRecord.body = raw;
      const payload = JSON.parse(raw);
      if (!payload.api_key) delete payload.api_key;
      Object.assign(state.vision, payload, { configured: true });
      return json(res, state.vision);
    }
    if (url.pathname === '/api/vision/test' && req.method === 'POST') {
      requestRecord.body = await readRequestBody(req);
      return json(res, { ok: true, message: 'mock test completed' });
    }
    if (url.pathname === '/api/tag' && req.method === 'POST') {
      requestRecord.body = await readRequestBody(req);
      return json(res, { job_ids: [9] }, 202);
    }
    if (url.pathname === '/api/jobs' && req.method === 'GET') return json(res, { jobs: state.jobs });
    if (url.pathname === '/api/jobs/7/retry' && req.method === 'POST') {
      state.jobs[0].status = 'queued';
      return json(res, { queued: true }, 202);
    }
    if (url.pathname === '/api/backup' && req.method === 'POST') {
      res.writeHead(200, { 'content-type': 'application/zip', 'content-disposition': 'attachment; filename="sticker-backup.zip"' });
      return res.end(Buffer.from('mock zip'));
    }
    if (url.pathname === '/api/restore' && req.method === 'POST') {
      await readRequestBody(req);
      return json(res, { imported: [state.item], duplicates: [] });
    }
    res.writeHead(404);
    res.end('not found');
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  return { server, requests, state, origin: `http://127.0.0.1:${server.address().port}` };
}

async function run() {
  const { server, requests, state, origin } = await startServer();
  let browser;
  try {
    browser = await chromium.launch({ headless: true });
    const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
    await page.goto(origin, { waitUntil: 'domcontentloaded' });
    await page.locator('#token').fill('demo-token');
    await page.getByRole('button', { name: '连接' }).click();
    await page.getByRole('heading', { name: '找到此刻最像你的那张' }).waitFor();
    await page.locator('.sticker-card').first().waitFor();
    await page.locator('.card-image').first().evaluate((image) => new Promise((resolve) => {
      const check = () => {
        if (image.src && image.complete) resolve();
        else window.requestAnimationFrame(check);
      };
      check();
    }));

    assert.equal(await page.locator('.sticker-card .card-title').textContent(), fixture.description);
    assert.equal(await page.locator('.sticker-card .card-title b').count(), 0, 'metadata must stay text, never become markup');
    assert.match(await page.locator('#gallery-status').textContent(), /1/);
    assert.equal(page.url(), origin + '/', 'the bearer token must not be written to the URL');
    assert.notEqual(await page.locator('#import-directory').getAttribute('webkitdirectory'), null);
    const assetRequest = requests.find((request) => request.path === '/assets/one');
    assert.equal(assetRequest.headers.authorization, 'Bearer demo-token');

    await page.locator('#search-query').fill('未标注');
    const searchResponse = page.waitForResponse((response) => response.url().includes('/api/stickers?'));
    await page.getByRole('button', { name: '搜索' }).click();
    await searchResponse;
    const searchRequest = requests.filter((request) => request.path === '/api/stickers').at(-1);
    assert.match(searchRequest.query, /q=%E6%9C%AA%E6%A0%87%E6%B3%A8/);

    await page.locator('.sticker-card').first().click();
    await page.getByRole('heading', { name: '编辑详情' }).waitFor();
    await page.locator('#field-description').fill('保存后的描述');
    await page.getByRole('button', { name: '保存修改' }).click();
    await page.getByText('已保存').waitFor();
    const patch = requests.find((request) => request.method === 'PATCH' && request.path === '/api/stickers/one');
    assert.equal(patch.headers.authorization, 'Bearer demo-token');
    assert.equal(patch.headers['x-csrf-token'], 'csrf-test-token');
    assert.deepEqual(Object.keys(JSON.parse(patch.body)), ['description'], 'only changed metadata fields should be marked manual');

    const singleTagResponse = page.waitForResponse((response) => response.url().endsWith('/api/tag'));
    await page.getByRole('button', { name: '识别此图' }).click();
    await singleTagResponse;
    const singleTag = requests.find((request) => request.path === '/api/tag');
    assert.deepEqual(JSON.parse(singleTag.body).ids, ['one']);

    await page.getByRole('button', { name: '喜欢', exact: true }).click();
    assert.ok(requests.some((request) => request.path === '/api/stickers/one/feedback'));
    await page.getByRole('button', { name: '软删除' }).click();
    await page.getByText('已移入回收站').waitFor();
    await page.locator('#include-deleted').check();
    await page.getByRole('button', { name: '恢复' }).click();
    await page.getByText('已恢复').waitFor();
    assert.equal(state.item.deleted, false);

    const settingsResponse = page.waitForResponse((response) => response.url().endsWith('/api/settings'));
    const visionSettingsResponse = page.waitForResponse((response) => response.url().endsWith('/api/vision'));
    await page.getByRole('tab', { name: '偏好设置' }).click();
    await Promise.all([settingsResponse, visionSettingsResponse]);
    await page.getByRole('heading', { name: '让助手更像你的习惯' }).waitFor();
    await page.locator('#setting-avoid-recent').fill('5');
    const settingsPatchResponse = page.waitForResponse((response) => response.url().endsWith('/api/settings'));
    await page.getByRole('button', { name: '保存偏好设置' }).click();
    await settingsPatchResponse;
    assert.equal(state.settings.avoid_recent, 5);
    await page.locator('#vision-api-key').fill('secret-value');
    const visionPatchResponse = page.waitForResponse((response) => response.url().endsWith('/api/vision'));
    await page.getByRole('button', { name: '保存视觉配置' }).click();
    await visionPatchResponse;
    const visionPatch = requests.find((request) => request.method === 'PATCH' && request.path === '/api/vision');
    assert.equal(JSON.parse(visionPatch.body).api_key, 'secret-value');
    assert.equal(visionPatch.headers['x-csrf-token'], 'csrf-test-token');
    const visionTestResponse = page.waitForResponse((response) => response.url().endsWith('/api/vision/test'));
    await page.getByRole('button', { name: /测试视觉配置/ }).click();
    await visionTestResponse;
    const visionTest = requests.find((request) => request.path === '/api/vision/test');
    assert.equal(JSON.parse(visionTest.body).confirm_cost, true);

    await page.getByRole('tab', { name: '图库' }).click();
    const pendingTagResponse = page.waitForResponse((response) => response.url().endsWith('/api/tag'));
    await page.getByRole('button', { name: '识别全部未标注' }).click();
    await pendingTagResponse;
    const pendingTag = requests.filter((request) => request.path === '/api/tag').at(-1);
    assert.equal(JSON.parse(pendingTag.body).all_pending, true);
    const input = page.locator('#import-files');
    await input.setInputFiles({ name: 'hello.png', mimeType: 'image/png', buffer: onePixel });
    await page.getByRole('button', { name: '开始导入' }).click();
    await page.getByText(/bad.gif/).waitFor();
    assert.ok(requests.some((request) => request.method === 'POST' && request.path === '/api/stickers'));
    await page.getByRole('button', { name: '刷新任务' }).click();
    await page.getByRole('button', { name: '重试' }).click();
    await page.getByText('排队中').waitFor();
    assert.equal(state.jobs[0].status, 'queued');
    await page.getByRole('button', { name: '下载备份' }).click();
    assert.ok(requests.some((request) => request.path === '/api/backup'));

    console.log('verify-ui: PASS');
  } finally {
    if (browser) await browser.close();
    await new Promise((resolve) => server.close(resolve));
  }
}

run().catch((error) => {
  console.error(`verify-ui: FAIL\n${error.stack || error}`);
  process.exitCode = 1;
});

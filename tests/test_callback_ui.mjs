import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';

// Exercise the actual page module with a small DOM and an in-memory HTTP server.
// No browser, microphone, providers, CRM or persisted personal data are involved.
class Element {
  constructor(id, attributes = '') {
    this.id = id;
    this.hidden = /\bhidden\b/.test(attributes);
    this.disabled = /\bdisabled\b/.test(attributes);
    this.value = '';
    this.textContent = '';
    this.children = [];
    this.classList = {toggle() {}};
  }
  replaceChildren(...children) { this.children = children; this.value = children[0]?.value || ''; }
  append(...children) { this.children.push(...children); }
  add(child) { this.children.push(child); }
  remove() {}
  scrollIntoView() {}
}

const operationId = '11111111-1111-4111-8111-111111111111';
const otherOperationId = '22222222-2222-4222-8222-222222222222';
const phone = '+12025550100';
const summary = 'Перезвонить вечером по поводу консультации.';
const candidates = [{id: 'contact1', name: 'Первый тестовый контакт'},
  {id: 'contact2', name: 'Второй тестовый контакт'}];
const draft = (overrides = {}) => ({id: operationId, status: 'draft', phone, summary,
  contact_id: 'contact2', contact_name: candidates[1].name, task_id: null, error: null, ...overrides});
const reply = (callback = null, callback_error = null) => ({text: 'Проверьте обращение.',
  mode: 'demo', latency_ms: 1, callback, callback_error});
const ambiguous = {code: 'ambiguous_contact', contacts: candidates, phone, summary};
const flush = async () => { for (let index = 0; index < 5; index++) await new Promise(setImmediate); };
let importId = 0;

async function page(t, handleRequest, remembered = null) {
  const elements = new Map();
  const html = readFileSync(new URL('../src/voicecrm/static/index.html', import.meta.url), 'utf8');
  for (const match of html.matchAll(/<[^>]+\bid="([^"]+)"[^>]*>/g)) elements.set(match[1], new Element(match[1], match[0]));
  const stored = new Map(remembered ? [['voicecrm.callback.id', remembered]] : []);
  const requests = [];
  const unexpected = [];
  const originals = new Map();
  const globals = {
    document: {getElementById: id => elements.get(id), createElement: () => new Element(),
      createTextNode: text => ({textContent: text})},
    window: {addEventListener() {}},
    navigator: {},
    Option: class { constructor(text, value) { this.textContent = text; this.value = value; } },
    localStorage: {getItem: key => stored.get(key) || null, setItem: (key, value) => stored.set(key, value),
      removeItem: key => stored.delete(key)},
    fetch: async (path, options = {}) => {
      const request = {path, method: options.method || 'GET',
        body: options.body ? JSON.parse(options.body) : undefined};
      requests.push(request);
      let response = await handleRequest?.(request);
      if (!response && path === '/health') response = {body: {mode: 'demo', stt_enabled: false, callback_enabled: true}};
      if (!response && path === '/sessions' && request.method === 'POST') response = {status: 201, body: {session_id: 'session1'}};
      if (!response) {
        unexpected.push(`${request.method} ${path}`);
        response = {status: 500, body: {detail: 'Unexpected synthetic request'}};
      }
      return new Response(response.status === 204 ? null : JSON.stringify(response.body),
        {status: response.status || 200, headers: {'Content-Type': 'application/json'}});
    },
  };
  for (const [name, value] of Object.entries(globals)) {
    originals.set(name, Object.getOwnPropertyDescriptor(globalThis, name));
    Object.defineProperty(globalThis, name, {configurable: true, writable: true, value});
  }
  t.after(() => {
    for (const [name, descriptor] of originals) {
      if (descriptor) Object.defineProperty(globalThis, name, descriptor);
      else delete globalThis[name];
    }
    assert.deepEqual(unexpected, [], 'All page requests must have explicit synthetic responses');
  });
  await import(new URL(`../src/voicecrm/static/app.js?callback-ui-test=${++importId}`, import.meta.url));
  await flush();
  const element = id => elements.get(id);
  return {element, requests, stored,
    async send(text) { element('text').value = text; element('form').onsubmit({preventDefault() {}}); await flush(); },
    async prepare() { element('callback-form').onsubmit({preventDefault() {}}); await flush(); },
    edit(id, value) { element(id).value = value; element(id).oninput(); },
    select(id) { element('callback-contact').value = id; element('callback-contact').onchange(); },
  };
}

test('ambiguous message opens the verified form and sends only the explicit human choice', async t => {
  const app = await page(t, request => {
    if (request.path.endsWith('/messages')) return {body: reply(null, ambiguous)};
    if (request.path === '/crm/callbacks') return {status: 201, body: {callback: draft(), text: 'Черновик подготовлен.'}};
  });
  await app.send('Прошу обратный звонок.');
  assert.equal(app.element('callback-request').open, true);
  assert.equal(app.element('callback-phone').value, phone);
  assert.equal(app.element('callback-summary').value, summary);
  assert.equal(app.element('callback-contact').hidden, false);
  assert.equal(app.element('callback-contact').required, true);
  assert.equal(app.element('callback-contact').value, '');
  assert.deepEqual(app.element('callback-contact').children.map(option => option.value), ['', 'contact1', 'contact2']);
  assert.equal(app.element('callback-prepare').disabled, true);
  assert.match(app.element('status').textContent, /Выберите клиента/);
  assert.equal(app.requests.filter(request => request.path === '/crm/callbacks').length, 0);

  // Even a programmatic submission cannot bypass the empty selection.
  await app.prepare();
  assert.equal(app.requests.filter(request => request.path === '/crm/callbacks').length, 0);
  app.select('contact2');
  assert.equal(app.element('callback-prepare').disabled, false);
  await app.prepare();
  assert.deepEqual(app.requests.find(request => request.path === '/crm/callbacks').body,
    {phone, summary, contact_id: 'contact2', session_id: 'session1', previous_id: null});
  assert.equal(app.element('callback-contact-name').textContent, `Контакт: ${candidates[1].name}`);
  assert.equal(app.element('callback-confirm').hidden, false);
  assert.equal(app.element('callback-contact').hidden, true);
});

test('editing either the phone or the wishes clears all candidates and the previous selection', async t => {
  const app = await page(t, request => request.path.endsWith('/messages') ? {body: reply(null, ambiguous)} : null);
  for (const [field, value] of [['callback-phone', '+12025550101'], ['callback-summary', 'Перезвонить завтра после обеда.']]) {
    await app.send('Уточнить контакт.');
    app.select('contact2');
    app.edit(field, value);
    assert.equal(app.element(field).value, value);
    assert.equal(app.element('callback-contact').hidden, true);
    assert.equal(app.element('callback-contact').required, false);
    assert.equal(app.element('callback-contact').value, '');
    assert.deepEqual(app.element('callback-contact').children, []);
    assert.equal(app.element('callback-contact-hint').hidden, true);
  }
});

test('HTTP ambiguity uses the same explicit selection and keeps the submitted wishes', async t => {
  const app = await page(t, request => request.path === '/crm/callbacks'
    ? {status: 409, body: {detail: {code: 'ambiguous_contact', message: 'Уточните клиента.', contacts: candidates}}} : null);
  app.edit('callback-phone', phone);
  app.edit('callback-summary', summary);
  await app.prepare();
  assert.equal(app.element('callback-phone').value, phone);
  assert.equal(app.element('callback-summary').value, summary);
  assert.equal(app.element('callback-contact').value, '');
  assert.equal(app.element('callback-prepare').disabled, true);
  assert.equal(app.element('status').textContent, 'Уточните клиента.');
});

test('contact preview distinguishes a known name, an unavailable name, and no contact', async t => {
  let operation = draft();
  const app = await page(t, request => request.path.endsWith('/messages') ? {body: reply(operation)} : null);
  for (const [contact_id, contact_name, expected] of [
    ['contact2', candidates[1].name, `Контакт: ${candidates[1].name}`],
    ['contact2', null, 'Контакт: contact2'],
    [null, null, 'Без привязки к контакту.'],
  ]) {
    operation = draft({contact_id, contact_name});
    await app.send('Подготовить черновик.');
    assert.equal(app.element('callback-contact-name').textContent, expected);
  }
});

test('a successful HTTP response still surfaces callback errors and removes stale candidates', async t => {
  let failure = ambiguous;
  const app = await page(t, request => request.path.endsWith('/messages') ? {body: reply(null, failure)} : null);
  await app.send('Подготовить обращение.');
  failure = {code: 'invalid_phone', contacts: []};
  await app.send('Исправить данные.');
  assert.equal(app.element('callback-contact').hidden, true);
  assert.match(app.element('status').textContent, /Подготовить обращение не удалось/);
  assert.equal(app.element('callback-card').hidden, true);
  assert.equal(app.requests.some(request => request.path === '/crm/callbacks'), false);
});

test('a proven missing callback is forgotten while the active session and wishes survive', async t => {
  let messages = 0;
  const app = await page(t, request => {
    if (request.path === `/crm/callbacks/${operationId}`) return {body: {callback: draft()}};
    if (request.path.endsWith('/messages')) return ++messages === 1
      ? {status: 404, body: {detail: {code: 'not_found', message: 'Обращение не найдено.', contacts: []}}}
      : {body: reply()};
  }, operationId);
  app.edit('callback-summary', 'Новые пожелания, которые нельзя потерять.');
  await app.send('Проверить результат.');
  assert.equal(app.stored.has('voicecrm.callback.id'), false);
  assert.equal(app.element('callback-card').hidden, true);
  assert.equal(app.element('callback-summary').value, 'Новые пожелания, которые нельзя потерять.');
  assert.equal(app.element('send').disabled, false);
  assert.equal(app.element('status').textContent, 'Обращение не найдено.');
  await app.send('Продолжить разговор.');
  assert.equal(app.requests.filter(request => request.path.endsWith('/messages'))[1].body.callback_id, null);
  assert.equal(app.requests.filter(request => request.path === '/sessions').length, 1);
});

test('a missing saved callback does not poison the new session', async t => {
  const app = await page(t, request => {
    if (request.path === `/crm/callbacks/${otherOperationId}`) return {status: 404,
      body: {detail: {code: 'not_found', message: 'Обращение не найдено.', contacts: []}}};
    if (request.path.endsWith('/messages')) return {body: reply()};
  }, otherOperationId);
  assert.equal(app.stored.has('voicecrm.callback.id'), false);
  assert.equal(app.element('send').disabled, false);
  await app.send('Новая реплика.');
  assert.equal(app.requests.find(request => request.path.endsWith('/messages')).body.callback_id, null);
});

test('malformed saved IDs are cleared without a callback request', async t => {
  const app = await page(t, request => request.path.endsWith('/messages') ? {body: reply()} : null, 'not-a-uuid');
  assert.equal(app.stored.has('voicecrm.callback.id'), false);
  assert.equal(app.requests.some(request => request.path.startsWith('/crm/callbacks/')), false);
  assert.equal(app.element('send').disabled, false);
  assert.match(app.element('status').textContent, /номер обращения некорректен/);
});

test('a missing session enables reset without pretending that the callback is missing', async t => {
  const app = await page(t, request => {
    if (request.path === `/crm/callbacks/${operationId}`) return {body: {callback: draft()}};
    if (request.path.endsWith('/messages')) return {status: 404, body: {detail: 'Session not found'}};
  }, operationId);
  await app.send('Продолжить разговор.');
  assert.equal(app.stored.get('voicecrm.callback.id'), operationId);
  assert.equal(app.element('send').disabled, true);
  assert.equal(app.element('reset').disabled, false);
  assert.match(app.element('status').textContent, /Сессия закончилась/);
});

test('unknown writes stay blocked and refreshing an error preserves edited wishes and the operation ID', async t => {
  const operation = draft({status: 'unconfirmed'});
  const app = await page(t, request => {
    if (request.path === `/crm/callbacks/${operationId}`) return {body: {callback: operation}};
    if (request.path.endsWith('/messages')) return {status: 503, body: {detail: 'Временно недоступно.'}};
  }, operationId);
  app.edit('callback-summary', 'Последние пожелания клиента.');
  assert.equal(app.element('callback-prepare').disabled, true);
  await app.send('Проверить результат.');
  assert.equal(app.stored.get('voicecrm.callback.id'), operationId);
  assert.equal(app.element('callback-summary').value, 'Последние пожелания клиента.');
  assert.equal(app.element('callback-prepare').disabled, true);
  assert.equal(app.element('callback-reconcile').hidden, false);
  assert.equal(app.requests.some(request => request.path === '/crm/callbacks'), false);
});

test('failed recovery after reload blocks new preparation and retries only a safe GET', async t => {
  let available = false;
  const app = await page(t, request => {
    if (request.path === `/crm/callbacks/${operationId}`) return available
      ? {body: {callback: draft({status: 'unconfirmed'}), text: 'Результат пока не подтверждён.'}}
      : {status: 503, body: {detail: 'CRM временно недоступна.'}};
  }, operationId);
  assert.equal(app.stored.get('voicecrm.callback.id'), operationId);
  assert.equal(app.element('callback-card').hidden, false);
  assert.equal(app.element('callback-reconcile').hidden, false);
  assert.equal(app.element('callback-confirm').hidden, true);
  assert.equal(app.element('callback-prepare').disabled, true);
  app.edit('callback-phone', phone);
  app.edit('callback-summary', 'Новые пожелания до восстановления связи.');
  await app.prepare();
  assert.equal(app.requests.some(request => request.path === '/crm/callbacks'), false);
  available = true;
  app.element('callback-reconcile').onclick();
  await flush();
  assert.equal(app.requests.filter(request => request.path === `/crm/callbacks/${operationId}`).length, 2);
  assert.ok(app.requests.filter(request => request.path === `/crm/callbacks/${operationId}`).every(request => request.method === 'GET'));
  assert.equal(app.element('callback-summary').value, 'Новые пожелания до восстановления связи.');
  assert.equal(app.element('callback-prepare').disabled, true);
  assert.equal(app.element('callback-reconcile').hidden, false);
});

test('network failure during recovery retains the UUID on subsequent dialogue requests', async t => {
  const app = await page(t, request => {
    if (request.path === `/crm/callbacks/${operationId}`) throw new Error('Synthetic connection failure');
    if (request.path.endsWith('/messages')) return {body: reply(draft({status: 'unconfirmed'}))};
  }, operationId);
  app.edit('callback-summary', 'Сохранить последние пожелания.');
  assert.equal(app.element('callback-prepare').disabled, true);
  await app.send('Проверить результат.');
  assert.equal(app.requests.find(request => request.path.endsWith('/messages')).body.callback_id, operationId);
  assert.equal(app.stored.get('voicecrm.callback.id'), operationId);
  assert.equal(app.element('callback-summary').value, 'Сохранить последние пожелания.');
  assert.equal(app.element('callback-prepare').disabled, true);
});

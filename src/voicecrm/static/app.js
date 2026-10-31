import {encodeWav} from './wav.js';

const el = id => document.getElementById(id);
const synth = window.speechSynthesis;
let session = null, busy = false, recording = null, sttEnabled = false, voices = [];
let pendingRecording = null;
let utterance = null, speechTiming = null, latestTiming = null;
let callback = null, callbackEnabled = false, callbackEdited = false;
let callbackContacts = [];
let callbackRecoveryId = null;
const callbackStorageKey = 'voicecrm.callback.id';
function savedCallback() { try { return localStorage.getItem(callbackStorageKey); } catch { return null; } }
function rememberCallback(id) { try { localStorage.setItem(callbackStorageKey, id); } catch {} }
function forgetCallback() {
  try { localStorage.removeItem(callbackStorageKey); } catch {}
  callbackRecoveryId = null;
  showCallback(null);
}
function clearCallbackContacts() {
  callbackContacts = [];
  el('callback-contact').replaceChildren();
  el('callback-contact').required = false;
  for (const id of ['callback-contact', 'callback-contact-label', 'callback-contact-hint']) el(id).hidden = true;
}
function callbackError(detail) {
  if (!detail || typeof detail !== 'object' || typeof detail.code !== 'string') return null;
  clearCallbackContacts();
  if (detail.code === 'ambiguous_contact' && Array.isArray(detail.contacts)) {
    callbackContacts = detail.contacts.filter(contact => contact && typeof contact.id === 'string' && contact.id);
    if (callbackContacts.length) {
      if (typeof detail.phone === 'string') el('callback-phone').value = detail.phone;
      if (typeof detail.summary === 'string') el('callback-summary').value = detail.summary;
      el('callback-contact').replaceChildren(new Option('Выберите клиента', ''),
        ...callbackContacts.map(contact => new Option(contact.name || contact.id, contact.id)));
      el('callback-contact').value = '';
      el('callback-contact').required = true;
      for (const id of ['callback-contact', 'callback-contact-label', 'callback-contact-hint']) el(id).hidden = false;
      el('callback-request').open = true;
      callbackEdited = true;
    }
  }
  return typeof detail.message === 'string' ? detail.message : detail.code === 'ambiguous_contact'
    ? 'Выберите клиента в форме и нажмите «Проверить обращение». Задача ещё не создана.'
    : 'Подготовить обращение не удалось. Проверьте данные; задача в CRM не подтверждена.';
}
function showCallback(value) {
  callback = value;
  callbackEdited = false;
  el('callback-card').hidden = !value;
  if (!value) return;
  rememberCallback(value.id);
  el('callback-status').textContent = ({draft: 'Проверьте обращение перед отправкой', succeeded: 'Задача создана в CRM',
    canceled: 'Черновик отменён', failed: 'Обращение не подтверждено', dispatching: 'Ожидаем результат CRM',
    unconfirmed: 'Результат CRM пока не подтверждён'})[value.status];
  el('callback-details').textContent = `${value.summary} · ${value.phone}`;
  el('callback-contact-name').textContent = !value.contact_id ? 'Без привязки к контакту.'
    : `Контакт: ${value.contact_name || value.contact_id}`;
  el('callback-receipt').textContent = value.task_id ? `Номер задачи: ${value.task_id}` : '';
  el('callback-confirm').hidden = value.status !== 'draft';
  el('callback-cancel').hidden = value.status !== 'draft';
  el('callback-reconcile').hidden = !['dispatching', 'unconfirmed'].includes(value.status);
}
function receiveCallback(value) {
  const preserveEdits = callbackEdited && value?.id === (callback?.id || callbackRecoveryId);
  callbackRecoveryId = null;
  showCallback(value);
  clearCallbackContacts();
  if (value && !preserveEdits) {
    el('callback-phone').value = value.phone;
    el('callback-summary').value = value.summary;
  }
  if (preserveEdits) callbackEdited = true;
}
function awaitCallbackRecovery(id) {
  callback = null;
  callbackRecoveryId = id;
  el('callback-card').hidden = false;
  el('callback-status').textContent = 'Сначала проверьте прежнее обращение';
  el('callback-details').textContent = 'Статус сохранённого обращения ещё не получен. Новое обращение пока недоступно.';
  el('callback-contact-name').textContent = '';
  el('callback-receipt').textContent = '';
  el('callback-confirm').hidden = true;
  el('callback-cancel').hidden = true;
  el('callback-reconcile').hidden = false;
}
const timingLabel = 'От отправки реплики до начала голоса';
const seconds = milliseconds => (milliseconds / 1000).toFixed(1).replace('.', ',');
function showTiming(timing, result = 'ожидаем голос…') {
  if (!timing || latestTiming !== timing) return;
  const stages = [];
  if (Number.isFinite(timing.sttMs)) stages.push(`STT ${seconds(timing.sttMs)} с`);
  if (Number.isFinite(timing.llmMs)) stages.push(`${timing.llmLabel} ${seconds(timing.llmMs)} с`);
  const total = Number.isFinite(timing.voiceMs) ? `${seconds(timing.voiceMs)} с` : result;
  el('latency').textContent = `${timingLabel}: ${total}${stages.length ? ' · ' + stages.join(' · ') : ''}`;
}
function beginTiming() {
  const timing = {submittedAt: performance.now(), pending: true};
  latestTiming = timing; showTiming(timing);
  return timing;
}
function status(text, error = false) {
  el('status').textContent = text;
  el('status').classList.toggle('error', error);
}
function controls() {
  for (const id of ['send', 'text', 'reset']) el(id).disabled = busy || !session || !!recording;
  el('file').disabled = busy || !session || !!recording || !sttEnabled;
  el('record').disabled = !session || !sttEnabled || (busy && !recording) || !navigator.mediaDevices?.getUserMedia;
  el('record').textContent = recording ? '■ Закончить запись' : '● Говорить';
  el('record').classList.toggle('recording', !!recording);
  for (const id of ['callback-prepare', 'callback-confirm', 'callback-cancel', 'callback-reconcile',
    'callback-phone', 'callback-summary', 'callback-contact']) {
    el(id).disabled = busy || !!recording || !callbackEnabled;
  }
  el('callback-confirm').disabled ||= callbackEdited;
  el('callback-prepare').disabled ||= !!callbackRecoveryId || ['dispatching', 'unconfirmed'].includes(callback?.status);
  el('callback-prepare').disabled ||= !el('callback-contact').hidden &&
    !callbackContacts.some(contact => contact.id === el('callback-contact').value);
}
async function request(path, options = {}) {
  let response;
  try { response = await fetch(path, {...options, signal: AbortSignal.timeout(80000)}); }
  catch { throw new Error('Не удалось получить ответ сервера. Проверьте соединение. Для отправленного обращения нажмите «Проверить результат».'); }
  if (!response.ok) {
    let body = {}; try { body = await response.json(); } catch {}
    const detail = typeof body.detail === 'string' ? body.detail : body.detail?.message || 'Проверьте сообщение или формат WAV.';
    const callbackMissing = response.status === 404 && body.detail?.code === 'not_found';
    if (callbackMissing) forgetCallback();
    if (response.status === 404 && path.startsWith('/sessions/') && !callbackMissing) {
      session = null;
      // Restore the ability to reset after server restart without silently losing context.
      throw new Error('Сессия закончилась. Нажмите «Новый диалог».');
    }
    const error = new Error(detail);
    error.callbackDetail = typeof body.detail === 'object' ? body.detail : null;
    throw error;
  }
  return response.status === 204 ? null : response.json();
}
function stopSpeech() {
  const timing = speechTiming;
  utterance = null; speechTiming = null;
  if (synth) synth.cancel();
  el('voice-status').textContent = '';
  if (timing?.pending) { timing.pending = false; showTiming(timing, 'не измерено — озвучивание остановлено'); }
}
function speak(text, timing = null) {
  if (!synth || !voices.length) {
    if (timing) { timing.pending = false; showTiming(timing, 'не измерено — голос недоступен'); }
    return status('Голос недоступен. Ответ можно прочитать в переписке.');
  }
  stopSpeech();
  speechTiming = timing;
  utterance = new SpeechSynthesisUtterance(text);
  const voice = voices.find(v => v.voiceURI === el('voice').value);
  if (voice) { utterance.voice = voice; utterance.lang = voice.lang; }
  const active = utterance;
  utterance.onstart = () => {
    if (utterance !== active) return;
    el('voice-status').textContent = 'Озвучиваю…';
    if (timing?.pending) {
      timing.voiceMs = performance.now() - timing.submittedAt;
      timing.pending = false; showTiming(timing);
    }
  };
  utterance.onend = () => {
    if (utterance !== active) return;
    speechTiming = null; el('voice-status').textContent = 'Озвучено';
    if (timing?.pending) { timing.pending = false; showTiming(timing, 'не измерено'); }
  };
  utterance.onerror = event => {
    if (utterance === active && timing?.pending) {
      timing.pending = false; showTiming(timing, 'не измерено — озвучивание не началось');
    }
    if (utterance === active && !['canceled', 'interrupted'].includes(event.error)) {
      el('voice-status').textContent = 'Нет звука';
      status('Озвучивание не удалось. Нажмите «Прослушать» у ответа.', true);
    }
  };
  synth.speak(utterance);
}
function loadVoices() {
  const previous = el('voice').value;
  voices = synth ? synth.getVoices() : [];
  voices.sort((a, b) => Number(b.lang.startsWith('ru')) - Number(a.lang.startsWith('ru')) || Number(b.localService) - Number(a.localService));
  el('voice').replaceChildren(...voices.map(v => new Option(`${v.name} · ${v.lang}${v.localService ? '' : ' · онлайн'}`, v.voiceURI)));
  if (voices.some(v => v.voiceURI === previous)) el('voice').value = previous;
  if (!voices.length) el('voice').add(new Option('Нет доступных голосов', ''));
  el('voice').disabled = !voices.length;
}
function message(role, text, timing) {
  el('empty')?.remove();
  const node = document.createElement('div'); node.className = `message ${role}`;
  const label = document.createElement('small'); label.textContent = role === 'user' ? 'Вы' : `Администратор${Number.isFinite(timing) ? ' · модель за ' + seconds(timing) + ' с' : ''}`;
  node.append(label, document.createTextNode(text));
  if (role !== 'user' && synth) {
    const play = document.createElement('button'); play.textContent = 'Прослушать';
    play.onclick = () => { if (!recording && !busy) speak(text); };
    node.append(play);
  }
  el('messages').append(node); node.scrollIntoView({block: 'nearest'});
}
async function send(text, timing = null) {
  if (callbackEdited && text.toLocaleLowerCase('ru').trim().replace(/[.!\s]+$/u, '') === 'подтверждаю заявку') {
    throw new Error('Данные обращения изменились. Сначала нажмите «Проверить обращение».');
  }
  status('Помощник отвечает…');
  timing ||= beginTiming();
  const data = await request(`/sessions/${session}/messages`, {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({text, callback_id: callback?.id || callbackRecoveryId || null})});
  timing.llmMs = data.latency_ms; timing.llmLabel = data.mode === 'openai' ? 'OpenAI' : 'Модель';
  showTiming(timing);
  message('user', text);
  message('assistant', data.text, data.latency_ms);
  receiveCallback(data.callback);
  el('text').value = '';
  const callbackFailure = callbackError(data.callback_error);
  status(callbackFailure || 'Можно продолжать разговор.', !!callbackFailure);
  if (el('speak').checked) speak(data.text, timing);
  else { timing.pending = false; showTiming(timing, 'не измерено — озвучивание выключено'); }
  if (callbackFailure) status(callbackFailure, true);
}
async function run(action) {
  if (busy) return;
  busy = true; stopSpeech(); controls();
  try { await action(); }
  catch (error) {
    if (callbackEnabled && callback?.id) {
      const edited = callbackEdited;
      try { showCallback((await request(`/crm/callbacks/${callback.id}`)).callback); } catch {}
      callbackEdited = edited;
    }
    const callbackFailure = callbackError(error.callbackDetail);
    status(callbackFailure || error.message, true);
    if (latestTiming?.pending) { latestTiming.pending = false; showTiming(latestTiming, 'не измерено — запрос не завершён'); }
  }
  finally { busy = false; controls(); if (!session) el('reset').disabled = false; }
}
async function newSession() {
  const health = await request('/health'); sttEnabled = health.stt_enabled;
  callbackEnabled = !!health.callback_enabled;
  el('callback-request').hidden = !callbackEnabled;
  el('connection').textContent = health.mode === 'openai' ? `OpenAI${health.llm_model ? ' · ' + health.llm_model : ''} · тестовая стоматология`
    : 'Демо · фиксированные ответы';
  if (session) {
    try { await request(`/sessions/${session}`, {method: 'DELETE'}); }
    catch (error) { if (session) throw error; }
  }
  session = null;
  const data = await request('/sessions', {method: 'POST'}); session = data.session_id;
  el('messages').replaceChildren(); el('text').value = '';
  latestTiming = null; el('latency').textContent = `${timingLabel}: —`;
  status('Нажмите «Говорить» или напишите сообщение.');
  const rememberedCallback = savedCallback();
  if (callbackEnabled && rememberedCallback) {
    if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(rememberedCallback)) {
      forgetCallback();
      status('Сохранённый номер обращения некорректен. Проверьте результат прежнего обращения в CRM перед новой отправкой.', true);
      return;
    }
    awaitCallbackRecovery(rememberedCallback);
    const restored = await request(`/crm/callbacks/${rememberedCallback}`);
    receiveCallback(restored.callback);
  }
}
async function transcribe(blob) {
  if (blob.size > 4 * 1024 * 1024) throw new Error('Максимальный размер WAV — 4 MiB.');
  status('Распознаём речь…');
  const timing = beginTiming();
  const data = await request('/transcriptions', {method: 'POST', headers: {'Content-Type': 'audio/wav'}, body: blob});
  timing.sttMs = data.latency_ms; showTiming(timing);
  if (!data.speech_detected) {
    timing.pending = false; showTiming(timing, 'не измерено — речь не обнаружена');
    return status('Речь не обнаружена. Попробуйте ещё раз.');
  }
  el('text').value = data.text;
  await send(data.text, timing);
}
function releaseRecording(current) {
  if (!current) return Promise.resolve();
  clearTimeout(current.timer); current.timer = null;
  current.stream?.getTracks().forEach(track => track.stop()); current.stream = null;
  if (current.node) current.node.port.onmessage = null;
  for (const key of ['source', 'node', 'silent']) {
    current[key]?.disconnect(); current[key] = null;
  }
  if (current.context && !current.closing) {
    current.closing = current.context.state === 'closed' ? Promise.resolve() : current.context.close().catch(() => {});
  }
  return current.closing || Promise.resolve();
}
async function startRecording() {
  if (busy || recording || pendingRecording) return;
  const current = {stream: null, context: null, node: null, source: null, silent: null, chunks: [], timer: null};
  pendingRecording = current;
  stopSpeech(); busy = true; controls();
  try {
    current.stream = await navigator.mediaDevices.getUserMedia({audio: {channelCount: 1, echoCancellation: true}});
    if (pendingRecording !== current) return;
    const context = current.context = new AudioContext({sampleRate: 24000});
    await context.resume();
    if (pendingRecording !== current) return;
    if (context.sampleRate !== 24000) throw new Error('Браузер не поддерживает запись 24 кГц. Используйте WAV-файл.');
    await context.audioWorklet.addModule('/static/recorder.js');
    if (pendingRecording !== current) return;
    const source = current.source = context.createMediaStreamSource(current.stream);
    const node = current.node = new AudioWorkletNode(context, 'pcm-recorder');
    const silent = current.silent = context.createGain(); silent.gain.value = 0;
    recording = current; pendingRecording = null;
    node.port.onmessage = async ({data}) => {
      if (recording !== current) return;
      if (data.samples) current.chunks.push(data.samples);
      if (!data.done) return;
      recording = null; pendingRecording = current;
      await releaseRecording(current);
      if (pendingRecording !== current) return;
      pendingRecording = null; busy = false;
      await run(async () => transcribe(encodeWav(current.chunks, 24000)));
    };
    source.connect(node); node.connect(silent); silent.connect(context.destination);
    current.timer = setTimeout(() => node.port.postMessage('stop'), 29000);
    status('Слушаю… Нажмите «Закончить запись». Максимум 29 секунд.');
    controls();
  } catch (error) {
    if (pendingRecording === current || recording === current) {
      pendingRecording = null; recording = null; busy = false;
      status(error.name === 'NotAllowedError' ? 'Разрешите доступ к микрофону или напишите сообщение.' : error.message, true);
      controls();
    }
  } finally {
    if (recording !== current) await releaseRecording(current);
  }
}
el('form').onsubmit = event => { event.preventDefault(); const text = el('text').value.trim(); if (text) run(() => send(text)); };
el('reset').onclick = () => run(newSession);
el('record').onclick = () => recording ? recording.node.port.postMessage('stop') : startRecording();
el('stop').onclick = stopSpeech;
el('speak').onchange = () => { if (!el('speak').checked) stopSpeech(); };
el('file').onchange = () => { const file = el('file').files[0]; el('file').value = ''; if (file) run(() => transcribe(file)); };
el('callback-form').onsubmit = event => {
  event.preventDefault();
  run(async () => {
    if (callbackRecoveryId || ['dispatching', 'unconfirmed'].includes(callback?.status)) {
      throw new Error('Сначала проверьте результат прежнего обращения. Повторная отправка пока недоступна.');
    }
    if (!el('callback-contact').hidden && !callbackContacts.some(contact => contact.id === el('callback-contact').value)) {
      throw new Error('Выберите клиента из списка перед подготовкой обращения.');
    }
    const data = await request('/crm/callbacks', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({phone: el('callback-phone').value, summary: el('callback-summary').value,
        contact_id: el('callback-contact').hidden ? null : el('callback-contact').value || null,
        session_id: session, previous_id: callback?.id || null})});
    receiveCallback(data.callback); status(data.text);
  });
};
for (const action of ['confirm', 'cancel', 'reconcile']) {
  el(`callback-${action}`).onclick = () => run(async () => {
    if (action === 'reconcile' && callbackRecoveryId) {
      const data = await request(`/crm/callbacks/${callbackRecoveryId}`);
      receiveCallback(data.callback); status(data.text);
      return;
    }
    if (!callback) return;
    await send(({confirm: 'Подтверждаю заявку', cancel: 'Отменяю заявку', reconcile: 'Проверить результат'})[action]);
  });
}
for (const id of ['callback-phone', 'callback-summary']) {
  el(id).oninput = () => { callbackEdited = true; clearCallbackContacts(); controls(); };
}
el('callback-contact').onchange = () => { callbackEdited = true; controls(); };
window.addEventListener('pagehide', () => {
  stopSpeech();
  const pending = pendingRecording, active = recording;
  pendingRecording = null; recording = null;
  releaseRecording(pending); releaseRecording(active);
  if (pending || active) { busy = false; controls(); }
  if (session) fetch(`/sessions/${session}`, {method: 'DELETE', keepalive: true}).catch(() => {});
});
loadVoices(); if (synth) synth.addEventListener('voiceschanged', loadVoices);
run(newSession);

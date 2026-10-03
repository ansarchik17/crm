import {encodeWav} from './wav.js';

const el = id => document.getElementById(id);
const synth = window.speechSynthesis;
let session = null, busy = false, recording = null, sttEnabled = false, voices = [];
let utterance = null;
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
}
async function request(path, options = {}) {
  let response;
  try { response = await fetch(path, {...options, signal: AbortSignal.timeout(80000)}); }
  catch { throw new Error('Не удалось получить ответ сервера. Проверьте соединение; при таймауте начните новый диалог.'); }
  if (!response.ok) {
    let body = {}; try { body = await response.json(); } catch {}
    const detail = typeof body.detail === 'string' ? body.detail : 'Проверьте сообщение или формат WAV.';
    if (response.status === 404 && path.startsWith('/sessions/')) {
      session = null;
      // Restore the ability to reset after server restart without silently losing context.
      throw new Error('Сессия закончилась. Нажмите «Новый диалог».');
    }
    throw new Error(detail);
  }
  return response.status === 204 ? null : response.json();
}
function stopSpeech() { if (synth) synth.cancel(); utterance = null; el('voice-status').textContent = ''; }
function speak(text) {
  if (!synth || !voices.length) return status('Голос недоступен. Ответ можно прочитать в переписке.');
  stopSpeech();
  utterance = new SpeechSynthesisUtterance(text);
  const voice = voices.find(v => v.voiceURI === el('voice').value);
  if (voice) { utterance.voice = voice; utterance.lang = voice.lang; }
  const active = utterance;
  utterance.onstart = () => { if (utterance === active) el('voice-status').textContent = 'Озвучиваю…'; };
  utterance.onend = () => { if (utterance === active) el('voice-status').textContent = 'Озвучено'; };
  utterance.onerror = event => {
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
  const label = document.createElement('small'); label.textContent = role === 'user' ? 'Вы' : `Администратор${timing ? ' · ответ за ' + (timing / 1000).toFixed(1) + ' с' : ''}`;
  node.append(label, document.createTextNode(text));
  if (role !== 'user' && synth) {
    const play = document.createElement('button'); play.textContent = 'Прослушать';
    play.onclick = () => { if (!recording && !busy) speak(text); };
    node.append(play);
  }
  el('messages').append(node); node.scrollIntoView({block: 'nearest'});
}
async function send(text) {
  status('Помощник отвечает…');
  const data = await request(`/sessions/${session}/messages`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({text})});
  message('user', text);
  message('assistant', data.text, data.latency_ms);
  el('text').value = '';
  status('Можно продолжать разговор.');
  if (el('speak').checked) speak(data.text);
}
async function run(action) {
  if (busy) return;
  busy = true; stopSpeech(); controls();
  try { await action(); }
  catch (error) { status(error.message, true); }
  finally { busy = false; controls(); if (!session) el('reset').disabled = false; }
}
async function newSession() {
  const health = await request('/health'); sttEnabled = health.stt_enabled;
  el('connection').textContent = health.mode === 'qwen' ? 'Qwen · тестовая стоматология' : 'Демо · фиксированные ответы';
  if (session) {
    try { await request(`/sessions/${session}`, {method: 'DELETE'}); }
    catch (error) { if (session) throw error; }
  }
  session = null;
  const data = await request('/sessions', {method: 'POST'}); session = data.session_id;
  el('messages').replaceChildren(); el('text').value = '';
  status('Нажмите «Говорить» или напишите сообщение.');
}
async function transcribe(blob) {
  if (blob.size > 4 * 1024 * 1024) throw new Error('Максимальный размер WAV — 4 MiB.');
  status('Распознаём речь…');
  const data = await request('/transcriptions', {method: 'POST', headers: {'Content-Type': 'audio/wav'}, body: blob});
  if (!data.speech_detected) return status('Речь не обнаружена. Попробуйте ещё раз.');
  el('text').value = data.text;
  await send(data.text);
}
async function startRecording() {
  stopSpeech(); busy = true; controls();
  let stream, context;
  try {
    stream = await navigator.mediaDevices.getUserMedia({audio: {channelCount: 1, echoCancellation: true}});
    context = new AudioContext({sampleRate: 24000});
    await context.resume();
    if (context.sampleRate !== 24000) throw new Error('Браузер не поддерживает запись 24 кГц. Используйте WAV-файл.');
    await context.audioWorklet.addModule('/static/recorder.js');
    const source = context.createMediaStreamSource(stream);
    const node = new AudioWorkletNode(context, 'pcm-recorder');
    const silent = context.createGain(); silent.gain.value = 0;
    const chunks = [];
    const current = {stream, context, node, source, silent, chunks, timer: null};
    recording = current;
    node.port.onmessage = async ({data}) => {
      if (data.samples) chunks.push(data.samples);
      if (!data.done || recording !== current) return;
      clearTimeout(current.timer); recording = null;
      stream.getTracks().forEach(t => t.stop()); source.disconnect(); node.disconnect(); silent.disconnect();
      await context.close(); busy = false;
      await run(async () => transcribe(encodeWav(chunks, 24000)));
    };
    source.connect(node); node.connect(silent); silent.connect(context.destination);
    current.timer = setTimeout(() => node.port.postMessage('stop'), 29000);
    status('Слушаю… Нажмите «Закончить запись». Максимум 29 секунд.');
  } catch (error) {
    stream?.getTracks().forEach(t => t.stop());
    if (context && context.state !== 'closed') await context.close();
    recording = null; busy = false;
    status(error.name === 'NotAllowedError' ? 'Разрешите доступ к микрофону или напишите сообщение.' : error.message, true);
  }
  controls();
}
el('form').onsubmit = event => { event.preventDefault(); const text = el('text').value.trim(); if (text) run(() => send(text)); };
el('reset').onclick = () => run(newSession);
el('record').onclick = () => recording ? recording.node.port.postMessage('stop') : startRecording();
el('stop').onclick = stopSpeech;
el('speak').onchange = () => { if (!el('speak').checked) stopSpeech(); };
el('file').onchange = () => { const file = el('file').files[0]; el('file').value = ''; if (file) run(() => transcribe(file)); };
window.addEventListener('pagehide', () => {
  stopSpeech();
  if (recording) { clearTimeout(recording.timer); recording.stream.getTracks().forEach(t => t.stop()); recording.context.close(); }
  if (session) fetch(`/sessions/${session}`, {method: 'DELETE', keepalive: true}).catch(() => {});
});
loadVoices(); if (synth) synth.addEventListener('voiceschanged', loadVoices);
run(newSession);

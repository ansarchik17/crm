import {RealtimeEvents} from './realtime-events.js';

const el = id => document.getElementById(id);
let config = null, current = null;
const bubbles = new Map();

function status(text, error = false) {
  el('status').textContent = text; el('status').classList.toggle('error', error);
}
function controls() {
  el('start').disabled = !!current || !config?.configured;
  el('end').disabled = !current;
  el('mute').disabled = !current?.ready;
  el('refresh').disabled = !!current;
  el('mute').textContent = current?.muted ? 'Включить микрофон' : 'Выключить микрофон';
}
async function refresh() {
  try {
    const response = await fetch('/realtime/config', {cache: 'no-store', signal: AbortSignal.timeout(5000)});
    if (!response.ok) throw new Error();
    config = await response.json();
    el('connection').textContent = `${config.model} · ${config.voice}`;
    el('vad').textContent = Number.isFinite(config.silence_duration_ms)
      ? `Пауза завершения речи: ${config.silence_duration_ms} мс` : 'Пауза завершения речи: —';
    el('setup').hidden = config.configured;
    status(config.configured ? 'Готово к подключению. Нажмите «Начать разговор».' : 'Нужен ключ OpenAI API. Живое подключение ещё не проверено.');
  } catch {
    config = null; el('setup').hidden = false;
    status('Сервер недоступен. Запустите его и нажмите «Проверить настройки».', true);
  }
  controls();
}
function stop(text = 'Разговор завершён. Микрофон выключен.', error = false) {
  const call = current;
  if (call) {
    for (const [id, item] of call.events.items) if (item.partial && item.role === 'assistant') call.events.interrupt(id);
  }
  current = null;
  if (call) {
    clearTimeout(call.setupTimer); clearInterval(call.durationTimer);
    call.abort.abort();
    call.stream?.getTracks().forEach(track => track.stop());
    call.channel?.close(); call.peer.close();
  }
  el('remote-audio').pause(); el('remote-audio').srcObject = null;
  el('play').hidden = true;
  status(text, error); controls();
}
function render(event, call) {
  if (current !== call) return;
  if (event.type === 'ready') {
    if (call.ready) return;
    call.ready = true; clearTimeout(call.setupTimer);
    call.stream.getAudioTracks().forEach(track => { track.enabled = true; });
    call.started = performance.now();
    status('Соединение установлено. Скажите, какая услуга вам нужна.');
    call.durationTimer = setInterval(() => {
      const remaining = Math.max(0, config.test_duration_seconds - Math.floor((performance.now() - call.started) / 1000));
      el('timer').textContent = `${Math.floor(remaining / 60)}:${String(remaining % 60).padStart(2, '0')}`;
      if (!remaining) stop('Пять минут теста завершены. Микрофон выключен.');
    }, 1000);
    controls();
  } else if (event.type === 'item') {
    el('empty')?.remove();
    let bubble = bubbles.get(event.id);
    if (!bubble) {
      const node = document.createElement('div'); node.className = `message ${event.role}`;
      const label = document.createElement('small'); label.textContent = event.role === 'user' ? 'Вы · расшифровка' : 'Администратор · OpenAI';
      const text = document.createElement('span'); node.append(label, text);
      bubble = {node, text}; bubbles.set(event.id, bubble); el('messages').append(node);
    }
    bubble.text.textContent = event.text;
    bubble.node.classList.toggle('partial', !!event.partial);
    bubble.node.classList.toggle('interrupted', !!event.interrupted);
    el('messages').scrollTop = el('messages').scrollHeight;
  } else if (event.type === 'status') {
    status(call.muted ? 'Микрофон выключен. Помощник может закончить ответ.' : event.text);
  } else if (event.type === 'latency') {
    const seconds = value => value === null ? '—' : `${(value / 1000).toFixed(2)} с`;
    el('latency').textContent = `Последний отклик сервиса: ${seconds(event.milliseconds)}`;
    el('latency-summary').textContent = `Ответов: ${event.count} · медиана: ${seconds(event.medianMilliseconds)} · p95: ${seconds(event.p95Milliseconds)}`;
  } else if (event.type === 'usage') {
    el('usage').textContent = `Расход токенов: ${event.totalTokens} · без расчёта стоимости`;
  } else if (event.type === 'error') status(event.text, true);
  else if (event.type === 'fatal') stop(event.text, true);
}
async function start() {
  if (current || !config?.configured) return;
  if (!window.RTCPeerConnection || !navigator.mediaDevices?.getUserMedia) {
    status('Нужен браузер с WebRTC и доступом к микрофону на localhost или HTTPS.', true); return;
  }
  const call = {peer: new RTCPeerConnection(), stream: null, channel: null, ready: false,
                muted: false, abort: new AbortController(), setupTimer: null, durationTimer: null};
  current = call;
  call.events = new RealtimeEvents(event => render(event, call));
  controls();
  status('Разрешите микрофон. Подключаемся к OpenAI…');
  call.setupTimer = setTimeout(() => {
    if (current === call) stop('Подключение заняло слишком долго. Попробуйте ещё раз.', true);
  }, 40000);
  try {
    // Acquire only after the explicit Start click; stop a late grant after cancellation.
    const stream = await navigator.mediaDevices.getUserMedia({audio: {echoCancellation: true, noiseSuppression: true, autoGainControl: true}});
    if (current !== call) { stream.getTracks().forEach(track => track.stop()); return; }
    call.stream = stream;
    el('messages').replaceChildren(); bubbles.clear();
    el('latency').textContent = 'Последний отклик сервиса: —';
    el('latency-summary').textContent = 'Ответов: 0 · медиана: — · p95: —';
    el('usage').textContent = 'Расход токенов: —'; el('timer').textContent = '';
    for (const track of stream.getAudioTracks()) {
      track.enabled = false;
      track.onended = () => { if (current === call) stop('Микрофон отключился. Разговор завершён.', true); };
      call.peer.addTrack(track, stream);
    }
    call.peer.ontrack = ({track, streams}) => {
      if (current !== call) return;
      el('remote-audio').srcObject = streams[0] || new MediaStream([track]);
      el('remote-audio').play().catch(() => { if (current === call) el('play').hidden = false; });
    };
    call.peer.onconnectionstatechange = () => {
      if (current === call && ['failed', 'disconnected', 'closed'].includes(call.peer.connectionState)) {
        stop('Соединение прервано. Микрофон выключен; можно начать заново.', true);
      }
    };
    call.channel = call.peer.createDataChannel('oai-events');
    call.channel.onmessage = ({data}) => {
      if (current !== call) return;
      try { call.events.handle(JSON.parse(data)); }
      catch { stop('Не удалось обработать событие Realtime. Разговор завершён.', true); }
    };
    call.channel.onclose = () => { if (current === call) stop('OpenAI завершил соединение. Микрофон выключен.'); };
    call.channel.onerror = () => { if (current === call) stop('Ошибка канала Realtime. Попробуйте снова.', true); };
    const offer = await call.peer.createOffer();
    if (current !== call) return;
    await call.peer.setLocalDescription(offer);
    const response = await fetch('/realtime/calls', {method: 'POST', headers: {'Content-Type': 'application/sdp'},
                                                  body: offer.sdp, signal: call.abort.signal});
    if (current !== call) return;
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new Error(typeof data.detail === 'string' ? data.detail : 'Не удалось подключиться к OpenAI.');
    }
    const sdp = await response.text();
    if (current === call) await call.peer.setRemoteDescription({type: 'answer', sdp});
  } catch (error) {
    if (current !== call) return;
    const text = error.name === 'NotAllowedError' ? 'Микрофон не разрешён. Разрешите доступ для этой страницы и повторите.'
      : error.name === 'NotFoundError' ? 'Микрофон не найден. Подключите его и повторите.'
      : error.message || 'Не удалось установить голосовое соединение.';
    stop(text, true);
  }
}
el('start').onclick = start;
el('end').onclick = () => stop();
el('refresh').onclick = refresh;
el('mute').onclick = () => {
  if (!current?.ready) return;
  current.muted = !current.muted;
  current.stream.getAudioTracks().forEach(track => { track.enabled = !current.muted; });
  status(current.muted ? 'Микрофон выключен. Нажмите «Включить микрофон», чтобы продолжить.' : 'Микрофон включён. Слушаю вас.');
  controls();
};
el('play').onclick = async () => {
  try { await el('remote-audio').play(); el('play').hidden = true; }
  catch { status('Браузер не воспроизводит звук. Проверьте разрешение звука для страницы.', true); }
};
window.addEventListener('pagehide', () => stop());
refresh();

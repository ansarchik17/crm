// Stateful event handling is separate from microphone/WebRTC and DOM rendering.
export class RealtimeEvents {
  constructor(emit, now = () => performance.now()) {
    this.emit = emit; this.now = now;
    this.items = new Map(); this.responses = new Map(); this.counted = new Set();
    this.itemResponses = new Map(); this.inactiveItems = new Set(); this.interruptedItems = new Set(); this.samples = new Map();
    this.pendingEnd = null; this.totalTokens = 0; this.activeItem = null; this.activeResponse = null;
  }
  item(id, role, text, partial = false) {
    const previous = this.items.get(id) || {role, text: '', interrupted: this.interruptedItems.has(id)};
    const item = {...previous, text, partial};
    this.items.set(id, item); this.emit({type: 'item', id, ...item});
  }
  interrupt(id) {
    if (!id) return;
    // Audio can be interrupted before the first transcript creates its screen item.
    this.interruptedItems.add(id);
    const item = this.items.get(id);
    if (!item) return;
    item.interrupted = true; item.partial = false;
    this.emit({type: 'item', id, ...item});
  }
  response(id) {
    if (!this.responses.has(id)) {
      this.responses.set(id, {end: null, milliseconds: null, status: null, invalid: false,
                              playbackStopped: false, items: new Set()});
    }
    return this.responses.get(id);
  }
  trackItem(event) {
    if (!event.response_id) return;
    this.itemResponses.set(event.item_id, event.response_id);
    this.response(event.response_id).items.add(event.item_id);
  }
  latencySummary() {
    const values = [...this.samples.values()];
    const sorted = [...values].sort((a, b) => a - b);
    const count = sorted.length;
    const middle = Math.floor(count / 2);
    return {milliseconds: values.at(-1) ?? null, count,
      medianMilliseconds: count ? (count % 2 ? sorted[middle] : (sorted[middle - 1] + sorted[middle]) / 2) : null,
      p95Milliseconds: count ? sorted[Math.ceil(count * 0.95) - 1] : null};
  }
  measure(id) {
    const response = this.responses.get(id);
    // A started buffer is only a candidate: a later failure/cancellation must not count.
    if (!response || response.invalid || response.status !== 'completed' ||
        response.milliseconds === null || this.samples.has(id)) return;
    this.samples.set(id, response.milliseconds);
    this.emit({type: 'latency', ...this.latencySummary()});
  }
  invalidate(id) {
    const response = this.responses.get(id);
    if (response) response.invalid = true;
    if (this.samples.delete(id)) this.emit({type: 'latency', ...this.latencySummary()});
  }
  finishPlayback(id, interrupted = false) {
    const response = this.responses.get(id);
    if (response) {
      response.playbackStopped = true;
      for (const itemId of response.items) {
        this.inactiveItems.add(itemId);
        if (interrupted) this.interrupt(itemId);
      }
    }
    if (interrupted) this.invalidate(id);
    if (!id || this.itemResponses.get(this.activeItem) === id) {
      if (this.activeItem) {
        this.inactiveItems.add(this.activeItem);
        if (interrupted) this.interrupt(this.activeItem);
      }
      this.activeItem = null;
    }
    if (!id || this.activeResponse === id) this.activeResponse = null;
  }
  stop() {
    // Generation may be completed while its audio is still playing or queued.
    // Keep only answers whose playback has actually finished.
    for (const [id, response] of this.responses) {
      if (!response.playbackStopped) this.finishPlayback(id, true);
    }
    this.finishPlayback(null, true);
    for (const [id, item] of this.items) {
      if (item.role === 'assistant' && item.partial && !this.inactiveItems.has(id)) this.interrupt(id);
    }
    this.pendingEnd = null;
  }
  handle(event) {
    switch (event.type) {
      case 'session.created':
        this.emit({type: 'ready'}); break;
      case 'input_audio_buffer.speech_started':
        this.pendingEnd = null;
        // The previous turn can still be waiting for audio, with no active buffer yet.
        for (const [id, response] of this.responses) if (!response.playbackStopped) this.invalidate(id);
        if (this.activeItem) this.inactiveItems.add(this.activeItem);
        this.interrupt(this.activeItem); this.activeItem = null;
        this.activeResponse = null;
        this.item(event.item_id, 'user', 'Слушаю…', true);
        this.emit({type: 'status', text: 'Слушаю вас…'}); break;
      case 'input_audio_buffer.speech_stopped':
        this.pendingEnd = this.now();
        this.item(event.item_id, 'user', 'Голосовая реплика · ожидаем расшифровку', true);
        this.emit({type: 'status', text: 'Помощник отвечает…'}); break;
      case 'conversation.item.input_audio_transcription.completed':
        this.item(event.item_id, 'user', event.transcript || 'Речь не распознана'); break;
      case 'conversation.item.input_audio_transcription.failed':
        this.item(event.item_id, 'user', 'Голосовая реплика · расшифровка недоступна'); break;
      case 'response.created': {
        if (!this.responses.has(event.response.id)) {
          this.response(event.response.id).end = this.pendingEnd;
          this.pendingEnd = null;
        }
        break;
      }
      case 'response.output_item.added':
        this.trackItem({response_id: event.response_id, item_id: event.item.id}); break;
      case 'response.output_audio_transcript.delta': {
        this.trackItem(event);
        const response = this.responses.get(event.response_id);
        if (!this.inactiveItems.has(event.item_id) && !response?.playbackStopped && !response?.invalid) {
          this.activeItem = event.item_id;
        }
        const current = this.items.get(event.item_id);
        this.item(event.item_id, 'assistant', (current?.text || '') + (event.delta || ''), true);
        break;
      }
      case 'response.output_audio_transcript.done':
        this.trackItem(event);
        // A late transcript must not reactivate an answer whose audio has stopped.
        if (event.response_id && this.activeResponse === event.response_id &&
            !this.responses.get(event.response_id)?.playbackStopped) this.activeItem = event.item_id;
        this.item(event.item_id, 'assistant', event.transcript || this.items.get(event.item_id)?.text || 'Голосовой ответ'); break;
      case 'conversation.item.truncated':
        this.inactiveItems.add(event.item_id);
        this.invalidate(this.itemResponses.get(event.item_id));
        this.interrupt(event.item_id); break;
      case 'output_audio_buffer.started': {
        const response = this.responses.get(event.response_id);
        if (response && !response.invalid && !response.playbackStopped) {
          this.activeResponse = event.response_id;
          this.activeItem = [...response.items].at(-1) || null;
          if (response.end !== null && response.milliseconds === null) {
            response.milliseconds = Math.max(0, this.now() - response.end);
            this.measure(event.response_id);
          }
          this.emit({type: 'status', text: 'Помощник говорит. Вы можете его перебить.'});
        }
        break;
      }
      case 'output_audio_buffer.stopped': {
        const id = event.response_id || this.activeResponse;
        const current = id && this.activeResponse === id &&
          (!this.activeItem || this.itemResponses.get(this.activeItem) === id);
        this.finishPlayback(id);
        if (current) this.emit({type: 'status', text: 'Слушаю. Можно продолжать разговор.'});
        break;
      }
      case 'output_audio_buffer.cleared':
        this.finishPlayback(event.response_id || this.activeResponse, true); break;
      case 'response.done': {
        const response = event.response;
        if (!this.counted.has(response.id)) {
          this.counted.add(response.id);
          this.totalTokens += response.usage?.total_tokens || 0;
          this.emit({type: 'usage', totalTokens: this.totalTokens});
        }
        const tracked = this.response(response.id);
        if (tracked.status !== null) break;
        tracked.status = response.status;
        if (response.status === 'failed') {
          this.invalidate(response.id);
          this.emit({type: 'error', text: 'OpenAI не смог сформировать ответ. Проверьте квоту API и повторите реплику.'});
        } else if (response.status === 'cancelled') {
          this.finishPlayback(response.id, true);
        } else if (response.status === 'incomplete') {
          this.invalidate(response.id);
          this.emit({type: 'error', text: 'Ответ оборвался до завершения. Попросите помощника ответить короче.'});
        } else this.measure(response.id);
        break;
      }
      case 'error':
        this.emit({type: 'fatal', text: 'Ошибка OpenAI Realtime. Разговор остановлен; проверьте доступ и квоту API.'}); break;
    }
  }
}

// Stateful event handling is separate from microphone/WebRTC and DOM rendering.
export class RealtimeEvents {
  constructor(emit, now = () => performance.now()) {
    this.emit = emit; this.now = now;
    this.items = new Map(); this.waiting = new Map(); this.counted = new Set();
    this.pendingEnd = null; this.totalTokens = 0; this.activeItem = null;
  }
  item(id, role, text, partial = false) {
    const previous = this.items.get(id) || {role, text: '', interrupted: false};
    const item = {...previous, text, partial};
    this.items.set(id, item); this.emit({type: 'item', id, ...item});
  }
  interrupt(id) {
    const item = this.items.get(id);
    if (!item) return;
    item.interrupted = true; item.partial = false;
    this.emit({type: 'item', id, ...item});
  }
  handle(event) {
    switch (event.type) {
      case 'session.created':
        this.emit({type: 'ready'}); break;
      case 'input_audio_buffer.speech_started':
        this.pendingEnd = null;
        this.interrupt(this.activeItem); this.activeItem = null;
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
      case 'response.created':
        if (this.pendingEnd !== null) this.waiting.set(event.response.id, this.pendingEnd);
        this.pendingEnd = null; break;
      case 'response.output_audio_transcript.delta': {
        this.activeItem = event.item_id;
        const current = this.items.get(event.item_id);
        this.item(event.item_id, 'assistant', (current?.text || '') + (event.delta || ''), true);
        break;
      }
      case 'response.output_audio_transcript.done':
        this.activeItem = event.item_id;
        this.item(event.item_id, 'assistant', event.transcript || this.items.get(event.item_id)?.text || 'Голосовой ответ'); break;
      case 'conversation.item.truncated':
        this.interrupt(event.item_id); break;
      case 'output_audio_buffer.started': {
        const end = this.waiting.get(event.response_id);
        if (end !== undefined) {
          this.emit({type: 'latency', milliseconds: Math.max(0, this.now() - end)});
          this.waiting.delete(event.response_id);
        }
        this.emit({type: 'status', text: 'Помощник говорит. Вы можете его перебить.'}); break;
      }
      case 'output_audio_buffer.stopped':
        this.activeItem = null;
        this.emit({type: 'status', text: 'Слушаю. Можно продолжать разговор.'}); break;
      case 'output_audio_buffer.cleared':
        this.interrupt(this.activeItem); this.activeItem = null; break;
      case 'response.done': {
        const response = event.response;
        if (!this.counted.has(response.id)) {
          this.counted.add(response.id);
          this.totalTokens += response.usage?.total_tokens || 0;
          this.emit({type: 'usage', totalTokens: this.totalTokens});
        }
        if (response.status === 'failed') {
          this.waiting.delete(response.id);
          this.emit({type: 'error', text: 'OpenAI не смог сформировать ответ. Проверьте квоту API и повторите реплику.'});
        } else if (response.status === 'cancelled') {
          this.waiting.delete(response.id); this.interrupt(this.activeItem);
        } else if (response.status === 'incomplete') {
          this.emit({type: 'error', text: 'Ответ оборвался до завершения. Попросите помощника ответить короче.'});
        }
        break;
      }
      case 'error':
        this.emit({type: 'fatal', text: 'Ошибка OpenAI Realtime. Разговор остановлен; проверьте доступ и квоту API.'}); break;
    }
  }
}

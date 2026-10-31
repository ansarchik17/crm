import {readFileSync} from 'node:fs';
import vm from 'node:vm';

class Element {
  constructor() {
    this.value = ''; this.children = []; this.hidden = false; this.checked = false;
    this.classList = {toggle() {}};
  }
  replaceChildren(...children) { this.children = children; }
  append(...children) { this.children.push(...children); }
  add(child) { this.children.push(child); }
  remove() {}
  scrollIntoView() {}
  pause() {}
  async play() {}
}

export const flush = async () => { for (let i = 0; i < 5; i++) await new Promise(setImmediate); };
export function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return {promise, resolve, reject};
}

// Run the real page in an isolated DOM/audio surface; never use a microphone,
// external network, credentials or the machine's browser globals.
export function browserPage(name, overrides = {}) {
  const elements = new Map(), listeners = new Map(), requests = [], timers = new Map();
  let timerId = 0;
  const element = id => {
    if (!elements.has(id)) elements.set(id, new Element());
    return elements.get(id);
  };
  const schedule = callback => { const id = ++timerId; timers.set(id, callback); return id; };
  const context = vm.createContext({
    document: {getElementById: element, createElement: () => new Element(), createTextNode: text => ({textContent: text})},
    window: {addEventListener(type, handler) { listeners.set(type, handler); }}, navigator: {},
    Option: class { constructor(text, value) { this.textContent = text; this.value = value; } },
    localStorage: {getItem() { return null; }, setItem() {}, removeItem() {}},
    AbortController, AbortSignal, performance,
    setTimeout: schedule, setInterval: schedule,
    clearTimeout: id => timers.delete(id), clearInterval: id => timers.delete(id),
    ...overrides,
    fetch: async (path, options = {}) => {
      requests.push({path, ...options});
      if (overrides.fetch) return overrides.fetch(path, options);
      const bodies = {
        '/health': {mode: 'demo', stt_enabled: true, callback_enabled: false},
        '/sessions': {session_id: 'synthetic-session'},
        '/transcriptions': {text: '', speech_detected: false, latency_ms: 1},
        '/realtime/config': {configured: true, model: 'synthetic', test_duration_seconds: 300},
      };
      if (path === '/realtime/calls') return new Response('v=0\nm=audio 9 UDP/TLS/RTP/SAVPF 111');
      if (options.method === 'DELETE') return new Response(null, {status: 204});
      if (!(path in bodies)) throw new Error(`Unexpected synthetic request: ${path}`);
      return new Response(JSON.stringify(bodies[path]));
    },
  });
  const source = readFileSync(new URL(`../../src/voicecrm/static/${name}.js`, import.meta.url), 'utf8');
  vm.runInContext(source.replace(/^import .*;\r?\n/gm, ''), context);
  return {element, requests, timers, context,
    evaluate: code => vm.runInContext(code, context),
    event: type => listeners.get(type)?.(),
  };
}

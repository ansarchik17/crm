import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {encodeWav} from '../src/voicecrm/static/wav.js';
import {browserPage, deferred, flush} from './helpers/browser.mjs';

test('WAV encodes mono PCM16 at 24 kHz, clamps samples and preserves chunk order', async () => {
  const blob = encodeWav([new Float32Array([-2, -0.5, 0]), new Float32Array([0.5, 2])], 24000);
  const bytes = await blob.arrayBuffer();
  const view = new DataView(bytes);
  assert.equal(blob.type, 'audio/wav');
  assert.equal(Buffer.from(bytes).toString('ascii', 0, 4), 'RIFF');
  assert.equal(view.getUint32(4, true), bytes.byteLength - 8);
  assert.equal(view.getUint16(20, true), 1);
  assert.equal(view.getUint16(22, true), 1);
  assert.equal(view.getUint32(24, true), 24000);
  assert.equal(view.getUint16(34, true), 16);
  assert.equal(view.getUint32(40, true), 10);
  assert.deepEqual(Array.from({length: 5}, (_, i) => view.getInt16(44 + i * 2, true)), [-32768, -16384, 0, 16384, 32767]);
});

test('empty recording does not create a WAV request', () => {
  assert.throws(() => encodeWav([], 24000), /пустая/);
});

function recorder() {
  let Processor;
  const messages = [];
  vm.runInNewContext(readFileSync(new URL('../src/voicecrm/static/recorder.js', import.meta.url), 'utf8'), {
    AudioWorkletProcessor: class { constructor() { this.port = {postMessage: data => messages.push(data)}; } },
    Float32Array, sampleRate: 24000,
    registerProcessor: (_, implementation) => { Processor = implementation; },
  });
  return {processor: new Processor(), messages};
}

test('recorder mixes channels, acknowledges stop once and ignores later samples', () => {
  const {processor, messages} = recorder();
  processor.process([[new Float32Array([1, 0]), new Float32Array([-1, 1])]]);
  assert.deepEqual(Array.from(messages[0].samples), [0, 0.5]);
  processor.port.onmessage({data: 'stop'});
  processor.port.onmessage({data: 'stop'});
  processor.process([[new Float32Array([1])]]);
  assert.equal(messages.length, 2);
  assert.equal(messages[1].done, true);
});

test('recorder stops at 29 seconds without exceeding the server limit', () => {
  const {processor, messages} = recorder();
  const input = [[new Float32Array(128)]];
  for (let i = 0; i < 6000; i++) processor.process(input);
  assert.equal(messages.reduce((sum, data) => sum + (data.samples?.length || 0), 0), 29 * 24000);
  assert.equal(messages.filter(data => data.done).length, 1);
});

function microphonePage({grant, resume, module, close} = {}) {
  const track = {stops: 0, stop() { this.stops++; }};
  const stream = {getTracks: () => [track]};
  const contexts = [], nodes = [];
  const page = browserPage('app', {encodeWav,
    navigator: {mediaDevices: {getUserMedia: () => grant?.promise || Promise.resolve(stream)}},
    AudioContext: class {
      constructor() {
        contexts.push(this); this.sampleRate = 24000; this.state = 'running'; this.closeCalls = 0;
        this.audioWorklet = {addModule: () => module?.promise || Promise.resolve()};
      }
      resume() { return resume?.promise || Promise.resolve(); }
      async close() { this.closeCalls++; await close?.promise; this.state = 'closed'; }
      createMediaStreamSource() { return {connect() {}, disconnect() {}}; }
      createGain() { return {gain: {}, connect() {}, disconnect() {}}; }
    },
    AudioWorkletNode: class {
      constructor() { nodes.push(this); this.port = {postMessage() {}}; }
      connect() {} disconnect() {}
    },
  });
  return {...page, track, stream, contexts, nodes};
}

test('leaving while microphone permission is pending stops a late grant without starting audio', async () => {
  const grant = deferred(), page = microphonePage({grant});
  await flush();
  const pending = page.evaluate('startRecording()');
  page.event('pagehide');
  grant.resolve(page.stream);
  await pending;
  assert.equal(page.track.stops, 1);
  assert.equal(page.contexts.length, 0);
  assert.equal(page.nodes.length, 0);
  assert.equal(page.requests.filter(request => request.path === '/transcriptions').length, 0);
});

for (const stage of ['resume', 'module']) {
  test(`leaving during audio ${stage} releases resources and ignores late completion`, async () => {
    const gate = deferred(), page = microphonePage({[stage]: gate});
    await flush();
    const pending = page.evaluate('startRecording()');
    await flush();
    page.event('pagehide');
    await flush();
    assert.equal(page.track.stops, 1, 'Stop the acquired microphone before the pending setup completes');
    gate.resolve();
    await pending;
    assert.equal(page.contexts[0].closeCalls, 1);
    assert.equal(page.nodes.length, 0);
    assert.equal(page.requests.filter(request => request.path === '/transcriptions').length, 0);
  });
}

test('leaving during recording ignores queued worklet events and does not upload audio', async () => {
  const page = microphonePage();
  await flush();
  await page.evaluate('startRecording()');
  const queued = page.nodes[0].port.onmessage;
  page.event('pagehide');
  await queued({data: {samples: new Float32Array([0.1]), done: true}});
  assert.equal(page.track.stops, 1);
  assert.equal(page.contexts[0].closeCalls, 1);
  assert.equal(page.requests.filter(request => request.path === '/transcriptions').length, 0);
  assert.equal(page.timers.size, 0);
});

test('leaving while the audio context closes cancels the pending transcription', async () => {
  const close = deferred(), page = microphonePage({close});
  await flush();
  await page.evaluate('startRecording()');
  const done = page.nodes[0].port.onmessage({data: {samples: new Float32Array([0.1]), done: true}});
  await flush();
  page.event('pagehide');
  close.resolve();
  await done;
  assert.equal(page.requests.filter(request => request.path === '/transcriptions').length, 0);
  assert.equal(page.contexts[0].closeCalls, 1);
});

test('normal recording still uploads one WAV and releases audio resources', async () => {
  const page = microphonePage();
  await flush();
  await page.evaluate('startRecording()');
  await page.nodes[0].port.onmessage({data: {samples: new Float32Array([0.1]), done: true}});
  const uploads = page.requests.filter(request => request.path === '/transcriptions');
  assert.equal(uploads.length, 1);
  assert.equal(uploads[0].body.type, 'audio/wav');
  assert.equal(page.track.stops, 1);
  assert.equal(page.contexts[0].closeCalls, 1);
  assert.equal(page.timers.size, 0);
});

for (const outcome of ['grant', 'deny']) {
  test(`a late microphone ${outcome} cannot change a newer recording after page restoration`, async () => {
    const grant = deferred(), page = microphonePage({grant});
    await flush();
    const previous = page.evaluate('startRecording()');
    page.event('pagehide');
    const nextTrack = {stops: 0, stop() { this.stops++; }};
    page.context.navigator.mediaDevices.getUserMedia = async () => ({getTracks: () => [nextTrack]});
    await page.evaluate('startRecording()');
    const status = page.element('status').textContent;
    if (outcome === 'grant') grant.resolve(page.stream);
    else grant.reject(Object.assign(new Error('Stale permission denied'), {name: 'NotAllowedError'}));
    await previous;
    assert.equal(page.element('status').textContent, status);
    assert.equal(page.contexts.length, 1);
    assert.equal(nextTrack.stops, 0);
    await page.nodes[0].port.onmessage({data: {samples: new Float32Array([0.1]), done: true}});
    assert.equal(nextTrack.stops, 1);
    assert.equal(page.requests.filter(request => request.path === '/transcriptions').length, 1);
  });
}

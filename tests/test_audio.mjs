import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {encodeWav} from '../src/voicecrm/static/wav.js';

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

import {test} from 'node:test';
import assert from 'node:assert/strict';
import {RealtimeEvents} from '../src/voicecrm/static/realtime-events.js';

test('transcript uses item IDs even when input transcription finishes after the answer', () => {
  const updates = [];
  const events = new RealtimeEvents(event => updates.push(event));
  events.handle({type: 'input_audio_buffer.speech_started', item_id: 'u1'});
  events.handle({type: 'input_audio_buffer.speech_stopped', item_id: 'u1'});
  events.handle({type: 'response.output_audio_transcript.delta', item_id: 'a1', delta: 'Какой '});
  events.handle({type: 'response.output_audio_transcript.delta', item_id: 'a1', delta: 'день?'});
  events.handle({type: 'conversation.item.input_audio_transcription.completed', item_id: 'u1', transcript: 'Хочу на чистку.'});
  assert.deepEqual([...events.items.keys()], ['u1', 'a1']);
  assert.equal(events.items.get('a1').text, 'Какой день?');
  assert.equal(events.items.get('u1').text, 'Хочу на чистку.');
});

test('latency is tied to the response and measured once at output buffer start', () => {
  let now = 100;
  const updates = [];
  const events = new RealtimeEvents(event => updates.push(event), () => now);
  events.handle({type: 'input_audio_buffer.speech_stopped', item_id: 'u1'});
  events.handle({type: 'response.created', response: {id: 'r1'}});
  now = 650;
  events.handle({type: 'output_audio_buffer.started', response_id: 'unrelated'});
  events.handle({type: 'output_audio_buffer.started', response_id: 'r1'});
  events.handle({type: 'output_audio_buffer.started', response_id: 'r1'});
  assert.deepEqual(updates.filter(e => e.type === 'latency'), [{type: 'latency', milliseconds: 550}]);
});

test('interruption stays marked after late transcript completion', () => {
  const events = new RealtimeEvents(() => {});
  events.handle({type: 'response.output_audio_transcript.delta', item_id: 'a1', delta: 'Добрый день'});
  events.handle({type: 'input_audio_buffer.speech_started', item_id: 'u2'});
  events.handle({type: 'response.output_audio_transcript.done', item_id: 'a1', transcript: 'Добрый день. Чем помочь?'});
  assert.equal(events.items.get('a1').interrupted, true);
});

test('usage is counted once and response failures are surfaced without raw data', () => {
  const updates = [];
  const events = new RealtimeEvents(event => updates.push(event));
  const response = {id: 'r1', status: 'failed', usage: {total_tokens: 17}, status_details: {error: {message: 'secret'}}};
  events.handle({type: 'response.done', response});
  events.handle({type: 'response.done', response});
  assert.equal(events.totalTokens, 17);
  assert.equal(updates.filter(e => e.type === 'usage').length, 1);
  assert.ok(updates.some(e => e.type === 'error'));
  events.handle({type: 'error', error: {message: 'secret'}});
  assert.ok(updates.some(e => e.type === 'fatal'));
  assert.ok(!JSON.stringify(updates).includes('secret'));
});

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
  now = 950;
  events.handle({type: 'output_audio_buffer.started', response_id: 'r1'});
  assert.equal(updates.filter(e => e.type === 'latency').length, 0);
  events.handle({type: 'response.done', response: {id: 'r1', status: 'completed'}});
  events.handle({type: 'response.done', response: {id: 'r1', status: 'completed'}});
  assert.deepEqual(updates.filter(e => e.type === 'latency'), [{type: 'latency', milliseconds: 550,
    count: 1, medianMilliseconds: 550, p95Milliseconds: 550}]);
});

test('latency supports response completion arriving before audio starts', () => {
  let now = 100;
  const events = new RealtimeEvents(() => {}, () => now);
  events.handle({type: 'input_audio_buffer.speech_stopped', item_id: 'u1'});
  events.handle({type: 'response.created', response: {id: 'r1'}});
  events.handle({type: 'response.done', response: {id: 'r1', status: 'completed'}});
  assert.equal(events.latencySummary().count, 0);
  now = 700;
  events.handle({type: 'output_audio_buffer.started', response_id: 'r1'});
  assert.equal(events.latencySummary().milliseconds, 600);
  assert.equal(events.latencySummary().count, 1);
});

test('summary uses arithmetic median and nearest-rank p95 for completed responses', () => {
  let now = 0;
  const events = new RealtimeEvents(() => {}, () => now);
  assert.deepEqual(events.latencySummary(), {milliseconds: null, count: 0,
    medianMilliseconds: null, p95Milliseconds: null});
  for (let i = 20; i > 0; i--) {
    events.handle({type: 'input_audio_buffer.speech_stopped', item_id: `u${i}`});
    events.handle({type: 'response.created', response: {id: `r${i}`}});
    now += i * 100;
    events.handle({type: 'output_audio_buffer.started', response_id: `r${i}`});
    events.handle({type: 'response.done', response: {id: `r${i}`, status: 'completed'}});
    events.handle({type: 'output_audio_buffer.stopped', response_id: `r${i}`});
  }
  assert.deepEqual(events.latencySummary(), {milliseconds: 100, count: 20,
    medianMilliseconds: 1050, p95Milliseconds: 1900});
});

test('cancelled, failed, incomplete, and unrelated responses never contribute to statistics', () => {
  let now = 0;
  const updates = [];
  const events = new RealtimeEvents(event => updates.push(event), () => now);
  for (const status of ['cancelled', 'failed', 'incomplete']) {
    events.handle({type: 'input_audio_buffer.speech_stopped', item_id: `u-${status}`});
    events.handle({type: 'response.created', response: {id: status}});
    now += 500;
    events.handle({type: 'output_audio_buffer.started', response_id: status});
    events.handle({type: 'response.done', response: {id: status, status}});
    events.handle({type: 'output_audio_buffer.started', response_id: status});
  }
  events.handle({type: 'response.created', response: {id: 'unsolicited'}});
  events.handle({type: 'output_audio_buffer.started', response_id: 'unsolicited'});
  events.handle({type: 'response.done', response: {id: 'unsolicited', status: 'completed'}});
  events.handle({type: 'output_audio_buffer.started', response_id: 'unknown'});
  events.handle({type: 'response.done', response: {id: 'unknown', status: 'completed'}});
  assert.equal(events.latencySummary().count, 0);
  assert.equal(updates.filter(e => e.type === 'latency').length, 0);
});

test('failure before audio start cannot later become a latency sample', () => {
  let now = 100;
  const events = new RealtimeEvents(() => {}, () => now);
  events.handle({type: 'input_audio_buffer.speech_stopped', item_id: 'u1'});
  events.handle({type: 'response.created', response: {id: 'r1'}});
  events.handle({type: 'response.done', response: {id: 'r1', status: 'failed'}});
  now = 700;
  events.handle({type: 'output_audio_buffer.started', response_id: 'r1'});
  assert.equal(events.latencySummary().count, 0);
});

test('clearing buffered audio retracts a previously completed generation from statistics', () => {
  let now = 100;
  const updates = [];
  const events = new RealtimeEvents(event => updates.push(event), () => now);
  events.handle({type: 'input_audio_buffer.speech_stopped', item_id: 'u1'});
  events.handle({type: 'response.created', response: {id: 'r1'}});
  now = 600;
  events.handle({type: 'output_audio_buffer.started', response_id: 'r1'});
  events.handle({type: 'response.done', response: {id: 'r1', status: 'completed'}});
  assert.equal(events.latencySummary().count, 1);
  events.handle({type: 'output_audio_buffer.cleared', response_id: 'r1'});
  assert.equal(events.latencySummary().count, 0);
  assert.deepEqual(updates.filter(e => e.type === 'latency').at(-1), {type: 'latency', milliseconds: null,
    count: 0, medianMilliseconds: null, p95Milliseconds: null});
});

test('interruption stays marked after late transcript completion', () => {
  const events = new RealtimeEvents(() => {});
  events.handle({type: 'response.output_audio_transcript.delta', item_id: 'a1', delta: 'Добрый день'});
  events.handle({type: 'input_audio_buffer.speech_started', item_id: 'u2'});
  events.handle({type: 'response.output_audio_transcript.done', item_id: 'a1', transcript: 'Добрый день. Чем помочь?'});
  assert.equal(events.items.get('a1').interrupted, true);
});

test('late transcript completion after playback stops is not interrupted by the next turn', () => {
  const events = new RealtimeEvents(() => {});
  events.handle({type: 'response.created', response: {id: 'r1'}});
  events.handle({type: 'response.output_audio_transcript.delta', response_id: 'r1', item_id: 'a1', delta: 'Добрый день'});
  events.handle({type: 'output_audio_buffer.started', response_id: 'r1'});
  events.handle({type: 'output_audio_buffer.stopped', response_id: 'r1'});
  events.handle({type: 'response.output_audio_transcript.done', response_id: 'r1', item_id: 'a1', transcript: 'Добрый день. Чем помочь?'});
  events.handle({type: 'input_audio_buffer.speech_started', item_id: 'u2'});
  assert.equal(events.items.get('a1').interrupted, false);
});

test('interruption before the first transcript stays marked when the transcript arrives late', () => {
  const events = new RealtimeEvents(() => {});
  events.handle({type: 'response.created', response: {id: 'r1'}});
  events.handle({type: 'response.output_item.added', response_id: 'r1', item: {id: 'a1'}});
  events.handle({type: 'output_audio_buffer.started', response_id: 'r1'});
  events.handle({type: 'input_audio_buffer.speech_started', item_id: 'u2'});
  events.handle({type: 'output_audio_buffer.cleared', response_id: 'r1'});
  assert.equal(events.items.has('a1'), false);
  events.handle({type: 'response.output_audio_transcript.done', response_id: 'r1', item_id: 'a1', transcript: 'Какой день вам подходит?'});
  assert.equal(events.items.get('a1').interrupted, true);
  assert.equal(events.activeItem, null);
});

test('speech starting before delayed audio preserves interruption when cancellation follows', () => {
  for (const audioBeforeCancellation of [true, false]) {
    let now = 100;
    const events = new RealtimeEvents(() => {}, () => now);
    events.handle({type: 'input_audio_buffer.speech_stopped', item_id: 'u1'});
    events.handle({type: 'response.created', response: {id: 'r1'}});
    events.handle({type: 'response.output_item.added', response_id: 'r1', item: {id: 'a1'}});
    events.handle({type: 'input_audio_buffer.speech_started', item_id: 'u2'});
    now = 500;
    if (audioBeforeCancellation) events.handle({type: 'output_audio_buffer.started', response_id: 'r1'});
    events.handle({type: 'response.done', response: {id: 'r1', status: 'cancelled'}});
    events.handle({type: 'output_audio_buffer.started', response_id: 'r1'});
    events.handle({type: 'response.output_audio_transcript.done', response_id: 'r1', item_id: 'a1', transcript: 'Какой день вам подходит?'});
    assert.equal(events.items.get('a1').interrupted, true);
    assert.equal(events.activeItem, null);
    assert.equal(events.latencySummary().count, 0);
  }
});

test('late audio from the previous turn cannot activate or interrupt the next answer', () => {
  let now = 100;
  const updates = [];
  const events = new RealtimeEvents(event => updates.push(event), () => now);
  events.handle({type: 'input_audio_buffer.speech_stopped', item_id: 'u1'});
  events.handle({type: 'response.created', response: {id: 'r1'}});
  events.handle({type: 'response.output_item.added', response_id: 'r1', item: {id: 'a1'}});
  events.handle({type: 'input_audio_buffer.speech_started', item_id: 'u2'});
  now = 500;
  events.handle({type: 'output_audio_buffer.started', response_id: 'r1'});
  assert.equal(events.activeResponse, null);
  assert.equal(updates.filter(event => event.type === 'status').at(-1).text, 'Слушаю вас…');
  events.handle({type: 'input_audio_buffer.speech_stopped', item_id: 'u2'});
  events.handle({type: 'response.created', response: {id: 'r2'}});
  events.handle({type: 'response.output_audio_transcript.delta', response_id: 'r2', item_id: 'a2', delta: 'Подберём другой день.'});
  events.handle({type: 'output_audio_buffer.cleared', response_id: 'r1'});
  events.handle({type: 'response.done', response: {id: 'r1', status: 'cancelled'}});
  assert.equal(events.activeItem, 'a2');
  assert.equal(events.items.get('a2').interrupted, false);
  now = 800;
  events.handle({type: 'output_audio_buffer.started', response_id: 'r2'});
  events.handle({type: 'output_audio_buffer.stopped', response_id: 'r1'});
  assert.equal(updates.filter(event => event.type === 'status').at(-1).text, 'Помощник говорит. Вы можете его перебить.');
  events.handle({type: 'response.done', response: {id: 'r2', status: 'completed'}});
  assert.equal(events.latencySummary().count, 1);
  assert.equal(events.latencySummary().milliseconds, 300);
  events.handle({type: 'output_audio_buffer.stopped', response_id: 'r2'});
  events.handle({type: 'input_audio_buffer.speech_started', item_id: 'u3'});
  assert.equal(events.latencySummary().count, 1);
});

test('clearing an old buffer leaves a newer active transcript untouched', () => {
  const events = new RealtimeEvents(() => {});
  events.handle({type: 'response.created', response: {id: 'r1'}});
  events.handle({type: 'response.output_item.added', response_id: 'r1', item: {id: 'a1'}});
  events.handle({type: 'output_audio_buffer.started', response_id: 'r1'});
  events.handle({type: 'response.created', response: {id: 'r2'}});
  events.handle({type: 'response.output_audio_transcript.delta', response_id: 'r2', item_id: 'a2', delta: 'Подберём другой день.'});
  events.handle({type: 'output_audio_buffer.cleared', response_id: 'r1'});
  assert.equal(events.activeItem, 'a2');
  assert.equal(events.items.get('a2').interrupted, false);
  assert.equal(events.activeResponse, null);
});

test('cancelling an unrelated response does not interrupt the active answer', () => {
  const events = new RealtimeEvents(() => {});
  events.handle({type: 'response.created', response: {id: 'r1'}});
  events.handle({type: 'response.created', response: {id: 'r2'}});
  events.handle({type: 'response.output_audio_transcript.delta', response_id: 'r1', item_id: 'a1', delta: 'Добрый день'});
  events.handle({type: 'output_audio_buffer.started', response_id: 'r1'});
  events.handle({type: 'response.done', response: {id: 'r2', status: 'cancelled'}});
  assert.equal(events.items.get('a1').interrupted, false);
  assert.equal(events.activeItem, 'a1');
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

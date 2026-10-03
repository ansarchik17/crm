export function encodeWav(chunks, sampleRate) {
  const length = chunks.reduce((sum, chunk) => sum + chunk.length, 0);
  if (!length) throw new Error('Запись пустая. Попробуйте ещё раз.');
  const data = new DataView(new ArrayBuffer(44 + length * 2));
  const write = (offset, text) => [...text].forEach((c, i) => data.setUint8(offset + i, c.charCodeAt(0)));
  write(0, 'RIFF'); data.setUint32(4, 36 + length * 2, true);
  write(8, 'WAVE'); write(12, 'fmt '); data.setUint32(16, 16, true);
  data.setUint16(20, 1, true); data.setUint16(22, 1, true);
  data.setUint32(24, sampleRate, true); data.setUint32(28, sampleRate * 2, true);
  data.setUint16(32, 2, true); data.setUint16(34, 16, true);
  write(36, 'data'); data.setUint32(40, length * 2, true);
  let offset = 44;
  for (const chunk of chunks) for (const value of chunk) {
    const s = Math.max(-1, Math.min(1, value));
    data.setInt16(offset, Math.round(s * (s < 0 ? 32768 : 32767)), true);
    offset += 2;
  }
  return new Blob([data.buffer], {type: 'audio/wav'});
}

import { afterEach, expect, it, vi } from 'vitest';
import { chooseKnowledgeFiles } from './api';

afterEach(() => vi.restoreAllMocks());

it('offers supported native media alongside documents and retains selection cap/cancel', async () => {
  let input: HTMLInputElement | undefined;
  vi.spyOn(HTMLInputElement.prototype, 'click').mockImplementation(function (this: HTMLInputElement) { input = this; });
  const picked = chooseKnowledgeFiles(1);
  expect(input?.accept.split(',')).toEqual(expect.arrayContaining(['.pdf', '.png', '.wav', '.mp3', '.flac', '.ogg', '.m4a', '.mp4', '.mov', '.webm', '.mkv']));
  const audio = new File(['public fixture'], 'sound.wav', { type: 'audio/wav' });
  Object.defineProperty(input, 'files', { value: [audio, new File(['x'], 'other.pdf')] });
  input!.dispatchEvent(new Event('change'));
  expect(await picked).toEqual([audio]);
  const cancelled = chooseKnowledgeFiles();
  input!.dispatchEvent(new Event('cancel'));
  expect(await cancelled).toEqual([]);
});

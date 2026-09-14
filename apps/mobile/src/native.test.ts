import { describe, expect, it } from 'vitest';
import { parseInvitation } from './native';
const valid = { endpoint: 'https://192.168.1.2:19705', certificate_sha256: 'a'.repeat(64), computer_id: 'computer', invitation: 'a'.repeat(43), expires_at: Date.now()/1000+300, protocol_version: 1 };
describe('pairing data from a QR code', () => {
  it('keeps the independently obtained computer fingerprint', () => {
    expect(parseInvitation(JSON.stringify(valid))).toEqual(valid);
  });
  it('accepts the same invitation from a PWA pairing link', () => {
    expect(parseInvitation(`https://192.168.1.2:19705/phone/#pair=${encodeURIComponent(JSON.stringify(valid))}`)).toEqual(valid);
  });
  it.each([{ endpoint: 'http://192.168.1.2:19705' }, { endpoint: 'https://user:password@example.com' },
    { endpoint: 'https://example.com/path' }, { certificate_sha256: '' }, { expires_at: 1 }, { protocol_version: 2 }])('rejects invalid trust data %j', change => {
    expect(() => parseInvitation(JSON.stringify({ ...valid, ...change }))).toThrow();
  });
});

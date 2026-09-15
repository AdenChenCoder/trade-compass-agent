import { Capacitor, registerPlugin } from '@capacitor/core';
import { BrowserCompass } from './browser';
import { PeerCompass, peerMode } from './peer';

export interface PairingInfo {
  endpoint: string;
  certificate_sha256: string;
  computer_id: string;
  invitation: string;
  protocol_version: number;
  expires_at: number;
}

export interface ConnectionInfo { connected: boolean; endpoint?: string; computer_id?: string }
const NativeCompass = registerPlugin<{
  pair(options: { invitation: string; name: string }): Promise<{ status: number; data: unknown }>;
  connection(): Promise<ConnectionInfo>;
  request(options: { method: string; path: string; body?: string }): Promise<{ status: number; data: unknown }>;
  forget(): Promise<void>;
}>('Compass');
export const Compass = Capacitor.isNativePlatform() ? NativeCompass : peerMode ? PeerCompass : BrowserCompass;

export class RequestError extends Error {
  constructor(public status: number, message: string) { super(message); }
}

export async function api<T>(path: string, method = 'GET', body?: unknown): Promise<T> {
  const result = await Compass.request({ path: `/mobile/v1/${path}`, method,
    ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
  if (result.status >= 400) {
    const detail = (result.data as { detail?: string }).detail;
    throw new RequestError(result.status, typeof detail === 'string' ? detail : '请求未完成');
  }
  return result.data as T;
}

export function parseInvitation(text: string): PairingInfo {
  if (text.trim().startsWith('https://')) {
    text = new URLSearchParams(new URL(text.trim()).hash.slice(1)).get('pair') || '';
  }
  const value = JSON.parse(text);
  const url = new URL(value.endpoint);
  if (value.protocol_version !== 1 || !/^[a-f0-9]{64}$/.test(value.certificate_sha256)
      || !/^[A-Za-z0-9_-]{43}$/.test(value.invitation) || typeof value.computer_id !== 'string'
      || url.protocol !== 'https:' || url.username || url.password || url.search || url.hash
      || url.pathname !== '/' || !Number.isFinite(value.expires_at) || value.expires_at * 1000 <= Date.now()) {
    throw new Error('连接信息无效或已过期，请在电脑上重新生成');
  }
  return value;
}

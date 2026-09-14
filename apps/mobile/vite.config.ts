import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { cpSync, readdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { createHash } from 'node:crypto';
const peer = process.env.VITE_COMPASS_TRANSPORT === 'peer';
export default defineConfig({ base: './', build: { outDir: peer ? 'dist-peer' : 'dist' }, plugins: [react(), {
  name: 'bundle-computer-pwa', apply: 'build',
  closeBundle() {
    const dist = resolve(import.meta.dirname, peer ? 'dist-peer' : 'dist');
    const assets = readdirSync(resolve(dist, 'assets')).map(name => `./assets/${name}`);
    const hash = createHash('sha256').update(assets.join('|'));
    for (const file of ['index.html', 'sw.js', 'manifest.webmanifest', 'icon.svg', 'icon-192.png', 'icon-512.png']) hash.update(readFileSync(resolve(dist, file)));
    const version = hash.digest('hex').slice(0, 12);
    const html = readFileSync(resolve(dist, 'index.html'), 'utf8')
      .replace('</head>', `<meta name="compass-version" content="${version}" /></head>`);
    writeFileSync(resolve(dist, 'index.html'), html);
    const sw = readFileSync(resolve(dist, 'sw.js'), 'utf8').replace('__VERSION__', version)
      .replace('__PEER_MODE__', JSON.stringify(peer))
      .replace('__PRECACHE__', JSON.stringify(['./', './index.html', './manifest.webmanifest', './icon.svg', './icon-192.png', './icon-512.png', ...assets]));
    writeFileSync(resolve(dist, 'sw.js'), sw);
    if (peer) return;
    const bundled = resolve(import.meta.dirname, '../../src/trade_compass_agent/mobile_dist');
    rmSync(bundled, { recursive: true, force: true }); cpSync(dist, bundled, { recursive: true });
  },
}], server: { port: 3001 } });

import React from 'react';
import { createRoot } from 'react-dom/client';
import { App } from './App';
import { prepareStartupUpdate } from './startup-update';
import { initializeAppearance } from '../../shared/appearance';
import '../../shared/appearance.css';
import './style.css';
initializeAppearance();
const root = createRoot(document.getElementById('root')!);
root.render(<div className="app-startup" role="status">正在打开交易罗盘…</div>);
void prepareStartupUpdate().then(reload => {
  if (reload) location.reload();
  else root.render(<React.StrictMode><App /></React.StrictMode>);
});

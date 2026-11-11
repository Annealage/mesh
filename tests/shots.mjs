// Shot table for Mesh's page (style/tools/shot, README "Checking a port").
//
// Serve a folder with three STL parts, an agent-mode run for most entries and a
// --no-agent run for the viewer-only ones:
//
//   .venv/bin/annealage-mesh /tmp/meshdemo --no-open --no-git --port 5733
//   .venv/bin/annealage-mesh /tmp/meshdemo --no-open --no-git --no-agent --port 5734
//   MESH_SHOT_TOKEN=<the #t= value the server printed> tools/shot --table tests/shots.mjs --base http://127.0.0.1:5733 --webgl --preset full --conform
//
// The viewer-only entries (`viewer-only-*`) belong to the second server; shoot
// them with `--base http://127.0.0.1:5734 --only <names>`.
//
// The scene is WebGL, so every entry is marked webgl. Entries are frame states:
// layout focus, the Panel closed, the phone's three tabs, the working line, an
// approval, a pause, Settings, sketch mode, a toast with the error line.

// The server refuses a headless browser its API without the login token it prints.
const TOKEN = process.env.MESH_SHOT_TOKEN;
const HOME = TOKEN ? `/#t=${TOKEN}` : '/';

const click = (selector) => async (tab) => {
  await tab.evaluate(`document.querySelector(${JSON.stringify(selector)}).click()`);
};
const both = (...steps) => async (tab) => { for (const step of steps) await step(tab); };

// Three parts, two pins and a callout, set through the page's own store.
const content = async (tab) => {
  await tab.evaluate(`(async () => {
    const s = window.mesh.store;
    s.addPin({ point: [0, 0, 0], part: 'alpha', label: '+Z' });
    s.addPin({ point: [5, 0, 0], part: 'alpha', label: '+X' });
    s.setCallouts([{ id: 1, part: 'alpha', label: '+Z', point: [0, 5, 0], comment: 'Top skin is 1.6 mm.' }]);
  })()`);
};
const approval = async (tab) => {
  await tab.evaluate(`(async () => {
    const { store } = await import('agent/store.js');
    store.addChatPermissionRequest('shot-1', 'Write', { file_path: 'cad/clip.py', content: 'NUT_SLOT = 5.7' }, null);
  })()`);
};
const working = async (tab) => {
  await tab.evaluate(`(async () => {
    const { store } = await import('agent/store.js');
    store.setChatUserTurn(1, [{ type: 'text', text: 'Take the side wall to 2.4 mm.' }]);
    store.setChatAgentStatus('ready');
  })()`);
};
const paused = click('#pauseBtn');
const sketch = click('#sketchBtn');
const settings = both(
  click('#settingsBtn'),
  async (tab) => {
    await tab.evaluate(`document.querySelectorAll('#settingsModal details').forEach((d) => { d.open = true; })`);
  },
);
const toastAndError = async (tab) => {
  await tab.evaluate(`(async () => {
    const ui = await import('agent/ui.js');
    ui.toast('Submitted 2 pins', true);
    ui.showError('No STLs found in the served directory.');
  })()`);
};

// [rail, centre, inspector, agent] as the grid resolves them; null is the fluid column.
const cols = (columns, reason) => ({ reason, expect: { '.wb': { gridTemplateColumns: columns } } });
const DRAWING = cols(['40px', null, '300px', '0px'], 'layout focus "drawing" closes the agent column');
const AGENT = cols(['0px', '0px', '0px', null], 'layout focus "agent" gives the agent column the whole window');
const INSPECTOR_CLOSED = cols(['40px', null, '0px', '400px'], 'the Panel closed collapses the inspector column');
const PHONE_TAB = { reason: 'a phone pane tab fills the page below the title bar; the desktop .vhead is not drawn', expect: { '.vhead': 'absent' } };

// Every listed part has its mesh, and the renderer has drawn since (resizing the canvas clears it).
const modelsLoaded = async () => {
  if (!window.mesh || window.mesh.store.getState().models.length === 0) return false;
  if (Object.keys(window.mesh.meshes).length !== window.mesh.store.getState().models.length) return false;
  await new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)));
  return true;
};

const frame = (name, extra) => ({ name, path: HOME, shape: 'workbench', webgl: true, ready: modelsLoaded, ...extra });
const wide = (name, extra) => frame(name, { viewports: [[1600, 1000]], ...extra });
const narrow = (name, extra) => frame(name, { viewports: [[400, 860]], ...extra });
const tab = (id) => click(`#tabbar [data-tab="${id}"]`);

export default {
  baseline: '../shots/accepted',
  allow: [
    { match: /^request (0|no response) .*\/model\/.*\.stl$/, reason: 'the first socket handshake refetches every part (a 304 each); the shot ends the page while that is in flight' },
    { match: /agent\/login\/providers/, reason: 'the shot servers run without an agent backend, so the Settings window asks for sign-in providers that do not exist' },
  ],
  shots: [
    wide('mesh-split', { state: content }),
    wide('mesh-drawing', { state: both(content, click('[data-layout-set="drawing"]')), conform: DRAWING }),
    wide('mesh-agent', { state: click('[data-layout-set="agent"]'), conform: AGENT }),
    wide('mesh-inspector-closed', { state: both(content, click('#panelBtn')), conform: INSPECTOR_CLOSED }),
    wide('mesh-working', { state: working, mask: ['#chatWorkingTime', '#sbWorkingTime'] }),
    wide('mesh-approval', { state: approval }),
    wide('mesh-paused', { state: paused }),
    wide('mesh-viewer-only', {}), // run against the --no-agent server
    wide('mesh-empty', { ready: () => !!window.mesh && !!document.getElementById('err').textContent }), // run against a server on an empty folder
    wide('mesh-settings', { state: settings }),
    wide('mesh-sketch', { state: sketch }),
    wide('mesh-toast-error', { state: both(content, toastAndError) }),
    narrow('mesh-phone-model'),
    narrow('mesh-phone-review', { state: both(content, tab('review')), conform: PHONE_TAB }),
    narrow('mesh-phone-agent', { state: tab('chat'), conform: PHONE_TAB }),
  ],
};

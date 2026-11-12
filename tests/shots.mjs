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
// `m5-title-odd` and `m5-title-odd-phone` also belong to a `--no-agent` server (the Agent column reads
// Unavailable, with no backend error in the chat), on a folder that holds one STL, `shroud.stl`, and has
// a 120-character name with markup characters in it. Do not start it with `--backend`: a backend the
// machine has not set up (codex without a login) puts its error text in the chat column.
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
// m2: two pins and a callout on real parts, a measurement between the pins, one pin selected.
const sceneState = async (tab) => {
  await tab.evaluate(`(async () => {
    const s = window.mesh.store;
    const [a, b] = s.getState().models;
    s.addPin({ point: [0, 0, 0], part: a.label, label: '+Z' });
    s.addPin({ point: [5, 0, 0], part: b.label, label: '+X' });
    s.setCallouts([{ id: 2, part: a.label, label: '+Z', point: [0, 5, 0], comment: 'Top skin is 1.6 mm.' }]);
    const pins = s.getState().pins;
    s.setMeasure('u' + pins[0].id, 'u' + pins[1].id);
    s.selectPin(pins[1].id);
  })()`);
};
// m2: a selected pin on the seventh part colour (teal, close to the temper), set on the 7-part server.
const part7State = async (tab) => {
  await tab.evaluate(`(async () => {
    const s = window.mesh.store;
    const m = s.getState().models[6];
    const g = window.mesh.meshes[m.rel].geometry;
    g.computeBoundingBox();
    const { min, max } = g.boundingBox;
    s.addPin({ point: [(min.x + max.x) / 2, (min.y + max.y) / 2, max.z], part: m.label, label: '+Z' });
    s.selectPin(s.getState().pins[0].id);
  })()`);
};
// m4: the review panel's rows. Three parts (the last hidden), two pins with comments (the second
// selected), one callout, a measurement from pin 1 to the callout.
const rows = async (tab) => {
  await tab.evaluate(`(async () => {
    const s = window.mesh.store;
    const models = s.getState().models;
    models.forEach((m, i) => s.setVisibility(m.rel, i < models.length - 1));
    s.addPin({ point: [0, 0, 0], part: models[0].label, label: '+Z' });
    s.addPin({ point: [-5, 3.25, 0], part: models[1].label, label: '-X' });
    const pins = s.getState().pins;
    s.setPinComment(pins[0].id, 'Wall is 1.6 mm here, the nut slot needs 2.4 mm.');
    s.setPinComment(pins[1].id, 'Chamfer this edge.');
    s.setCallouts([{ id: 2, part: models[0].label, label: '+Z', point: [0, 5, 0], comment: 'Top skin is 1.6 mm.' }]);
    s.setMeasure('u' + pins[0].id, 'a2');
    s.selectPin(pins[1].id);
  })()`);
};
// Twelve long-named parts and eight pins, set on the 12-part server.
const many = async (tab) => {
  await tab.evaluate(`(async () => {
    const s = window.mesh.store;
    const models = s.getState().models;
    for (let i = 0; i < 8; i++) {
      s.addPin({ point: [i * 2.5 - 9, (i % 3) * 3 - 3, 5], part: models[i].label, label: '+Z' });
    }
    s.setPinComment(s.getState().pins[0].id, 'First of eight.');
    s.selectPin(s.getState().pins[3].id);
  })()`);
};
const longCallouts = async (tab) => {
  await tab.evaluate(`(async () => {
    const s = window.mesh.store;
    const part = s.getState().models[0].label;
    const long = 'The side wall is only 1.2 mm where the clip seats, which will crack when the clip is pushed in. '.repeat(6).slice(0, 600);
    s.setCallouts([
      { id: 1, part, label: '+X', point: [10, 0, 0], comment: long },
      { id: 2, part, label: '+Z', point: [0, 0, 10] },
    ]);
  })()`);
};
const submitted = async (tab) => {
  await tab.evaluate(`(async () => {
    const s = window.mesh.store;
    const part = s.getState().models[0].label;
    s.addPin({ point: [0, 0, 0], part, label: '+Z' });
    s.addPin({ point: [5, 0, 0], part, label: '+X' });
    document.getElementById('submit').click();
    for (let i = 0; i < 100 && s.getState().dirty; i++) await new Promise((r) => setTimeout(r, 50));
  })()`);
};
const clearAll = both(
  async (tab) => {
    await tab.evaluate(`(async () => {
      const s = window.mesh.store;
      const part = s.getState().models[0].label;
      s.addPin({ point: [0, 0, 0], part, label: '+Z' });
      s.addPin({ point: [5, 0, 0], part, label: '+X' });
    })()`);
  },
  click('#clear'),
);
const strokes = async (tab) => {
  await tab.evaluate(`document.querySelector('#sketchBtn').click()`);
  await tab.evaluate(`(() => {
    const o = document.querySelector('.sketch-overlay');
    const r = o.getBoundingClientRect();
    const fire = (type, px, py) => o.dispatchEvent(new PointerEvent(type, {
      bubbles: true, pointerId: 1, pointerType: 'mouse', isPrimary: true, button: 0, buttons: type === 'pointerup' ? 0 : 1,
      clientX: r.x + r.width * px, clientY: r.y + r.height * py,
    }));
    const draw = (pts) => {
      fire('pointerdown', ...pts[0]);
      pts.slice(1).forEach((p) => fire('pointermove', ...p));
      fire('pointerup', ...pts[pts.length - 1]);
    };
    draw([[0.3, 0.3], [0.4, 0.25], [0.5, 0.2], [0.6, 0.27], [0.7, 0.35]]);
    draw([[0.3, 0.7], [0.4, 0.75], [0.5, 0.8], [0.6, 0.72], [0.7, 0.65]]);
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
// m5: the words that need a failure to show. Each calls the same ui helper the real path does with
// the string the real path uses; the failure itself (a deleted STL, a refused Submit, a moved
// view) is not staged, which a headless table cannot do between two frames.
const say = (kind, text, ok) => async (tab) => {
  await tab.evaluate(`(async () => {
    const ui = await import('agent/ui.js');
    ui.${kind}(${JSON.stringify(text)}${ok === undefined ? '' : ', ' + ok});
  })()`);
};
const paletteDisabled = async (tab) => {
  await tab.evaluate(`window.annealagePalette.open({ query: 'submit' })`);
};
const toastAndError = async (tab) => {
  await tab.evaluate(`(async () => {
    const ui = await import('agent/ui.js');
    ui.toast('Submitted 2 pins', true);
    ui.showError('No parts in this folder yet.');
  })()`);
};

// The account menu of a hosted title bar. Mesh does not serve itself hosted yet, so the page never loads
// account.js: these states stand in for the host. They answer /_front/me the way the hosted front does (a
// signed-in person, or a visitor with no principal: 401) and load the vendored script the way a hosted page
// would, so the title bar is shown with its place filled in.
const ME = { v: 1, display: 'andrew', email: 'andrew@example.com', account_url: 'https://account.annealage.ai/', security_url: 'https://account.annealage.ai/security', projects_url: 'https://account.annealage.ai/projects', signout: { method: 'POST', url: '/auth/logout' }, products: [{ key: 'mesh', name: 'Mesh', url: 'https://mesh.annealage.ai/', current: true }, { key: 'canvas', name: 'Canvas', url: 'https://canvas.annealage.ai/', current: false }, { key: 'loom', name: 'Loom', url: 'https://loom.annealage.ai/', current: false }] };
const MENU = '.titlebar .tb-right > [data-account-menu] [data-menu]';
const hosted = (visitor) => async (tab) => {
  await tab.run(async ({ page }, o) => {
    await page.route('**/_front/me', (r) => r.fulfill({ status: o.visitor ? 401 : 200, contentType: 'application/json', body: JSON.stringify(o.visitor ? { error: 'unauthenticated' } : o.me) }));
    await page.evaluate(() => new Promise((done, fail) => {
      const s = document.createElement('script');
      s.src = '/style/account.js';
      s.onload = done;
      s.onerror = fail;
      document.body.appendChild(s);
    }));
    if (o.visitor) await new Promise((done) => setTimeout(done, 400));
    else await page.waitForSelector(o.menu, { timeout: 8000 });
  }, { args: [{ visitor: !!visitor, me: ME, menu: MENU }] });
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

// A wide shot and its phone twin, which opens the Review tab.
const review = (name, state, extra = {}) => [
  wide(name, { state, ...extra }),
  narrow(name + '-phone', { state: both(state, tab('review')), conform: PHONE_TAB, ...extra }),
];

export default {
  baseline: '../shots/accepted',
  allow: [
    { match: /^request (0|no response) .*\/model\/.*\.stl$/, reason: 'the first socket handshake refetches every part (a 304 each); the shot ends the page while that is in flight' },
    { match: /_front\/me/, reason: 'no principal: the snippet draws nothing' },
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
    wide('mesh-empty', { ready: () => !!window.mesh && !document.getElementById('viewEmpty').hidden }), // run against a server on an empty folder
    wide('mesh-settings', { state: settings }),
    wide('mesh-sketch', { state: sketch }),
    wide('mesh-toast-error', { state: both(content, toastAndError) }),
    frame('m2-scene', { state: sceneState }),
    frame('m2-sketch', { state: both(sceneState, strokes) }),
    frame('m2-part7', { state: part7State }), // run against a server on a folder of seven STLs
    ...review('m4-rows', rows),
    ...review('m4-empty', async () => {}),
    ...review('m4-many', many), // run against a server on a folder of twelve long-named STLs
    ...review('m4-callouts', longCallouts),
    ...review('m4-submitted', submitted, { mask: ['#toast'] }),
    ...review('m4-clear', clearAll),
    wide('m5-load-failed', { state: both(content, say('showError', 'Could not load alpha.')) }),
    wide('m5-pin-toast', { state: both(content, click('#panelBtn'), say('toast', 'Pin 3 added. Open the review panel to comment.', true)), conform: INSPECTOR_CLOSED }),
    wide('m5-submit-refused', { state: both(content, say('toast', 'Could not submit: sign in again', false)) }),
    wide('m5-sketch-moved', { state: both(sketch, say('toast', 'The view moved since you started drawing, so the marks no longer line up. Clear and draw again, or put the view back.', false)) }),
    wide('m5-palette-disabled', { state: paletteDisabled }),
    wide('m5-title-odd', {}), // run against the --no-agent server on the one-STL folder with a hostile 120-character name
    narrow('m5-title-odd-phone'), // same server
    // hosted title bar: the account menu closed and open, at 1100 wide (the name hides below 1200), and a visitor with no menu
    wide('mesh-hosted-menu', { state: both(content, hosted()) }),
    wide('mesh-hosted-open', { state: both(content, hosted(), click(MENU)) }),
    frame('mesh-hosted-1100', { viewports: [[1100, 800]], state: both(content, hosted(), click(MENU)) }),
    wide('mesh-hosted-visitor', { state: both(content, hosted(true)) }),
    narrow('mesh-hosted-menu-phone', { state: hosted() }),
    narrow('mesh-hosted-open-phone', { state: both(hosted(), click(MENU)) }),
    narrow('mesh-hosted-visitor-phone', { state: hosted(true) }),
    narrow('m7-phone-approval', { state: approval }), // the compact status bar carries the approval alert on the Model tab
    narrow('mesh-phone-model'),
    narrow('mesh-phone-review', { state: both(content, tab('review')), conform: PHONE_TAB }),
    narrow('mesh-phone-agent', { state: tab('chat'), conform: PHONE_TAB }),
  ],
};

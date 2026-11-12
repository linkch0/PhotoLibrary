/**
 * Exercise app.js in a minimal fake DOM to prove the library-switch reset works.
 *
 * Runs the real app.js (not a copy), with just enough DOM stubs for the
 * functions under test. Focus: after switchLibrary/applyLibrary, no filter
 * from the previous library may survive.
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const src = fs.readFileSync(path.join(__dirname, '..', 'app', 'static', 'app.js'), 'utf8');

/* ---- minimal DOM ------------------------------------------------------- */
function makeEl(tag = 'div') {
  const el = {
    tagName: tag, className: '', textContent: '', innerHTML: '', value: '',
    dataset: {}, style: {}, children: [], href: '', title: '',
    _listeners: {},
    classList: {
      _s: new Set(),
      add(c) { this._s.add(c); },
      remove(c) { this._s.delete(c); },
      toggle(c, on) { on ? this._s.add(c) : this._s.delete(c); },
      contains(c) { return this._s.has(c); },
    },
    appendChild(c) { this.children.push(c); return c; },
    addEventListener(ev, fn) { (this._listeners[ev] ||= []).push(fn); },
    getBoundingClientRect() { return { top: 0, left: 0, right: 0, bottom: 0, width: 100, height: 100 }; },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    closest() { return null; },
    scrollIntoView() {},
    focus() {},
    get firstChild() { return this.children[0] || null; },
  };
  return el;
}

const registry = new Map();
function id(sel) {
  if (!registry.has(sel)) registry.set(sel, makeEl());
  return registry.get(sel);
}

const document = {
  querySelector: (sel) => id(sel),
  querySelectorAll: () => [],
  createElement: (t) => makeEl(t),
  createDocumentFragment: () => makeEl('fragment'),
  documentElement: { style: { setProperty() {} } },
  addEventListener() {},
  activeElement: null,
  body: makeEl('body'),
};

const responses = {};
function fetchStub(url, opts) {
  const u = typeof url === 'string' ? url : url.url;
  for (const key of Object.keys(responses)) {
    if (u.includes(key)) {
      return Promise.resolve({
        ok: true,
        status: 200,
        json: async () => responses[key],
        text: async () => JSON.stringify(responses[key]),
      });
    }
  }
  return Promise.resolve({ ok: true, status: 200, json: async () => ({}) });
}

const sandbox = {
  document,
  window: { addEventListener() {}, innerWidth: 1400, innerHeight: 900 },
  fetch: fetchStub,
  console,
  setTimeout, clearTimeout, requestAnimationFrame: (fn) => setTimeout(fn, 0),
  URLSearchParams,
  navigator: { clipboard: { writeText: async () => {} } },
  IntersectionObserver: class { observe() {} disconnect() {} },
  localStorage: { getItem: () => null, setItem() {} },
  location: { hash: '' },
};
sandbox.globalThis = sandbox;
vm.createContext(sandbox);

// Expose internals for assertions: append a small export hook.
const hooked = src + '\n;globalThis.__t = { state, applyLibrary, renderYears, openLibrary };';
vm.runInContext(hooked, sandbox, { filename: 'app.js' });

const { state, applyLibrary, renderYears } = sandbox.__t;

/* ---- assertions -------------------------------------------------------- */
let failures = 0;
function check(name, cond, extra) {
  if (cond) {
    console.log(`  PASS  ${name}`);
  } else {
    failures++;
    console.log(`  FAIL  ${name}${extra ? '  ' + extra : ''}`);
  }
}

(async () => {
  // Simulate the state left behind by library A.
  state.query = 'IMG_99';
  state.year = '2022';
  state.albumId = 53;
  state.view = 'album:53';
  state.items = [{ id: 1 }, { id: 2 }];
  state.offset = 2;
  state.done = true;
  state.lastMonth = '2022-05';
  state.viewerIndex = 4;
  document.querySelector('#search').value = 'IMG_99';
  document.querySelector('#year').value = '2022';

  const libB = {
    name: 'Library B',
    stats: { years: { '2026': 2306 }, visible: 2306, assets: 2306 },
    albums: [],
    warnings: [],
  };

  responses['/api/assets'] = { total: 2306, offset: 0, limit: 300, items: [] };
  responses['/api/timeline'] = { years: { '2026': { '09': 2306 } } };

  await applyLibrary(libB);

  console.log('after switching to library B:');
  check('year filter reset', state.year === '', `got ${JSON.stringify(state.year)}`);
  check('search term reset', state.query === '', `got ${JSON.stringify(state.query)}`);
  check('album cleared', state.albumId === null);
  check('view reset to all', state.view === 'all', `got ${state.view}`);
  check('items replaced', state.items.length === 0);
  check('offset reset', state.offset === 0);
  check('done reset', state.done === false);
  check('lastMonth reset', state.lastMonth === '');
  check('search box cleared', document.querySelector('#search').value === '');
  check('lib name shown', document.querySelector('#libName').textContent === 'Library B');

  // renderYears must not keep a selection absent from the new library.
  const sel = document.querySelector('#year');
  sel.value = '2022';
  const kept = renderYears({ '2026': 5 }, true);
  check('renderYears drops missing year', kept === '' && sel.value === '', `got ${kept}`);

  sel.value = '2026';
  const kept2 = renderYears({ '2026': 5 }, true);
  check('renderYears keeps valid year', kept2 === '2026', `got ${kept2}`);

  const cleared = renderYears({ '2026': 5 }, false);
  check('renderYears ignores selection when asked', cleared === '', `got ${cleared}`);

  console.log(`\nfailures: ${failures}`);
  console.log('RESULT:', failures === 0 ? 'PASS' : 'FAIL');
  process.exit(failures === 0 ? 0 : 1);
})();

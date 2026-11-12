/**
 * Verify mediaUrl produces library-versioned URLs, and that a library switch
 * changes them (so the browser cannot serve stale cached thumbnails).
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const src = fs.readFileSync(path.join(__dirname, '..', 'app', 'static', 'app.js'), 'utf8');

function makeEl(tag = 'div') {
  return {
    tagName: tag, className: '', textContent: '', innerHTML: '', value: '',
    dataset: {}, style: {}, children: [], href: '', title: '', src: '',
    classList: { _s: new Set(), add(c){this._s.add(c);}, remove(c){this._s.delete(c);},
                 toggle(c,o){o?this._s.add(c):this._s.delete(c);}, contains(c){return this._s.has(c);} },
    appendChild(c) { this.children.push(c); return c; },
    addEventListener() {}, getBoundingClientRect(){ return {top:0,left:0,right:0,bottom:0,width:1,height:1}; },
    querySelector(){ return null; }, querySelectorAll(){ return []; }, closest(){ return null; },
    scrollIntoView(){}, focus(){}, get firstChild(){ return this.children[0] || null; },
  };
}

const registry = new Map();
const document = {
  querySelector: (s) => { if (!registry.has(s)) registry.set(s, makeEl()); return registry.get(s); },
  querySelectorAll: () => [], createElement: (t) => makeEl(t),
  createDocumentFragment: () => makeEl('fragment'),
  documentElement: { style: { setProperty() {} } }, addEventListener() {},
  activeElement: null, body: makeEl('body'),
};

const sandbox = {
  document,
  window: { addEventListener() {}, innerWidth: 1400, innerHeight: 900 },
  fetch: () => Promise.resolve({ ok: true, status: 200, json: async () => ({}) }),
  console, setTimeout, clearTimeout,
  requestAnimationFrame: (fn) => setTimeout(fn, 0),
  URLSearchParams,
  navigator: { clipboard: { writeText: async () => {} } },
  IntersectionObserver: class { observe() {} disconnect() {} },
  localStorage: { getItem: () => null, setItem() {} },
  location: { hash: '' },
};
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(src + '\n;globalThis.__t = { state, mediaUrl };', sandbox, { filename: 'app.js' });

const { state, mediaUrl } = sandbox.__t;

let failures = 0;
const check = (name, cond, extra) => {
  if (cond) console.log(`  PASS  ${name}`);
  else { failures++; console.log(`  FAIL  ${name}${extra ? '  ' + extra : ''}`); }
};

// Library A
state.lib = { token: 'aaaa111122' };
const thumbA = mediaUrl('thumb', 846, 'size=s');
const previewA = mediaUrl('preview', 846);
const videoA = mediaUrl('video', 846);
const origA = mediaUrl('original', 846, 'download=true');

console.log('library A:');
console.log('  ', thumbA);
check('thumb has size', thumbA.includes('size=s'), thumbA);
check('thumb has lib token', thumbA.includes('lib=aaaa111122'), thumbA);
check('preview has token', previewA.includes('lib=aaaa111122'), previewA);
check('video has token', videoA.includes('lib=aaaa111122'), videoA);
check('original keeps download', origA.includes('download=true'), origA);
check('original has token', origA.includes('lib=aaaa111122'), origA);
check('single ? in query', (thumbA.match(/\?/g) || []).length === 1, thumbA);

// Same id, different library -> different URL (this is the bug fix)
state.lib = { token: 'bbbb333344' };
const thumbB = mediaUrl('thumb', 846, 'size=s');
console.log('library B:');
console.log('  ', thumbB);
check('same id yields a different URL', thumbA !== thumbB, `${thumbA} vs ${thumbB}`);
check('URL contains new token', thumbB.includes('lib=bbbb333344'), thumbB);
check('old token absent', !thumbB.includes('aaaa111122'), thumbB);

// No token (library not yet loaded) must still yield a valid URL.
state.lib = null;
const bare = mediaUrl('thumb', 1, 'size=s');
check('no token still valid', bare === '/api/thumb/1?size=s', bare);

console.log(`\nfailures: ${failures}`);
console.log('RESULT:', failures === 0 ? 'PASS' : 'FAIL');
process.exit(failures === 0 ? 0 : 1);

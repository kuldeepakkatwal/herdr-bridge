// Checks planEdits (app.js) the way the iPhone app's InputSyncTests do: replay each plan on a model of
// Claude's bar and compare the end result. Run: node bridge/web/inputsync.test.js
'use strict';
const fs = require('fs');
const assert = require('assert');
const src = fs.readFileSync(require('path').join(__dirname, 'app.js'), 'utf8');
const body = src.split('// --- planEdits')[1].split('// --- end planEdits')[0];
const planEdits = new Function(body.slice(body.indexOf('\n')) + '\nreturn planEdits;')();

function apply(s, caret, plan) {
  const t = [...s];
  let c = caret;
  for (const e of plan) {
    if (e.text !== undefined) { t.splice(c, 0, ...e.text); c += [...e.text].length; continue; }
    for (const k of e.keys) {
      if (k === 'left') c = Math.max(0, c - 1);
      else if (k === 'right') c = Math.min(t.length, c + 1);
      else if (k === 'backspace') { if (c > 0) { t.splice(c - 1, 1); c -= 1; } }
      else throw new Error('unexpected key ' + k);
    }
  }
  return [t.join(''), c];
}
const check = (old, oc, nw, nc) => assert.deepStrictEqual(apply(old, oc, planEdits(old, oc, nw, nc)), [nw, nc], `${old}@${oc} -> ${nw}@${nc}`);

assert.deepStrictEqual(planEdits('/sta', 4, '/stat', 5), [{ text: 't' }]);                       // typing at the end
assert.deepStrictEqual(planEdits('status', 6, 'status', 2), [{ keys: ['left', 'left', 'left', 'left'] }]);   // cursor move only
check('stus', 2, 'status', 4);                 // insert in the middle after a space-bar drag
check('/statusline', 7, '/statline', 5);       // delete in the middle
check('fix the bug now', 15, 'fix the tests now', 13);   // replace a word
check('/stat', 5, '/statusline ', 12);         // fill a suggestion
check('hello', 5, '', 0);                      // clear all
check('aa', 1, 'aaa', 2);                      // repeated letters
check('café 😀', 6, 'café 😀!', 7);            // code points, not UTF-16 units
console.log('inputsync: all checks passed');

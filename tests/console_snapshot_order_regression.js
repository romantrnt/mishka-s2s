'use strict';
const assert = require('assert'), fs = require('fs'), vm = require('vm');
const source = fs.readFileSync(process.argv[2] || 'app/foundation/console/console.js', 'utf8');
const start = source.indexOf('function simulatorSnapshotIsCurrent(');
const end = source.indexOf('\nfunction render(', start);
const context = vm.createContext({current: null});
vm.runInContext(source.slice(start, end), context);
const state = (revision, cursor = 22, epoch = 9) => ({simulator: {
  epoch, journal_cursor: cursor, ...(revision === undefined ? {} : {snapshot_revision: revision}),
}});
const accepts = (next, previous) => context.simulatorSnapshotIsCurrent(next, previous);
// HTTP A was captured before WS B, even though lighting changed without an input event.
const httpA = state(30), wsB = state(31);
assert(accepts(wsB, httpA), 'a new animation frame with an unchanged journal must advance');
assert(!accepts(httpA, wsB), 'late HTTP must not roll lighting or other state back after WS');
assert(accepts(state(32), wsB), 'successive background frames keep animating');
assert(accepts(wsB, wsB), 'redelivery of the identical snapshot remains harmless');
assert(!accepts(state(29, 23), wsB), 'a newer journal cannot rescue an older captured snapshot');
assert(!accepts(state(32, 21), wsB), 'presentation ordering does not weaken journal fencing');
assert(!accepts(state(99, 99, 8), wsB), 'the previous epoch never returns');
assert(accepts(state(1, 23, 10), wsB), 'a new cleanup epoch still owns the boundary');
assert(!accepts(state(undefined), wsB), 'a versioned server cannot regress to an unversioned response');
assert(!accepts(state(undefined, 23, 10), wsB), 'a new epoch cannot remove presentation ordering');
const instance = (id, revision) => {const value=state(revision,0,0);value.simulator.instance_id=id;return value;};
assert(!accepts(instance('b'.repeat(16),1),instance('a'.repeat(16),500)), 'a different server instance cannot silently replace current state');
assert(!accepts(state(501,0,0),instance('a'.repeat(16),500)), 'server identity cannot disappear after upgrade');
assert(!accepts(instance('invalid',501),instance('a'.repeat(16),500)), 'malformed server identities fail closed');
for (const invalid of [-1, 1.5, '32', Number.MAX_SAFE_INTEGER + 1]) {
  assert(!accepts(state(invalid), wsB), 'malformed presentation revisions are rejected');
}
assert(accepts(state(1), state(undefined)), 'a new client can upgrade its legacy snapshot');
assert(accepts(state(undefined, 23), state(undefined)), 'legacy snapshots retain journal ordering');
assert(!accepts(state(undefined, 21), state(undefined)), 'legacy snapshots still reject old journals');
console.log('panel snapshot ordering across delayed HTTP, WS animation, epoch and legacy boundaries passed');

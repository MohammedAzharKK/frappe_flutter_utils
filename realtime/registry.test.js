const { test } = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const { createHash } = require("node:crypto");
const { DeviceSocketRegistry } = require("./registry");

const hash = (value) => createHash("sha256").update(value).digest("hex");
function socket(site, key, secret = "secret", user = "user") {
	const result = new EventEmitter();
	result.nsp = { name: `/${site}` };
	result.user = user;
	result.authorization_header = `FlutterDevice ${key}:${secret}`;
	result.connected = true;
	result.disconnect = (closeTransport) => {
		assert.equal(closeTransport, false);
		result.connected = false;
		result.emit("disconnect");
	};
	return result;
}

test("revocation disconnects only matching site/device, including all its sockets", () => {
	const registry = new DeviceSocketRegistry();
	const sockets = [socket("a", "key"), socket("a", "key"), socket("a", "other"), socket("b", "key")];
	sockets.forEach((item) => registry.add(item));
	registry.disconnect({ site: "a", key_hash: hash("key") });
	assert.deepEqual(sockets.map((item) => item.connected), [false, false, true, true]);
	assert.equal(registry.sockets.size, 2);
});

test("rotation disconnects only the old secret generation", () => {
	const registry = new DeviceSocketRegistry();
	const old = socket("a", "key", "old");
	const fresh = socket("a", "key", "fresh");
	registry.add(old);
	registry.add(fresh);
	registry.disconnect({ site: "a", key_hash: hash("key"), generation: hash("key:old") });
	assert.equal(old.connected, false);
	assert.equal(fresh.connected, true);
});

test("disabled user disconnects all their managed devices on that site only", () => {
	const registry = new DeviceSocketRegistry();
	const sockets = [socket("a", "one"), socket("a", "two"), socket("b", "one"), socket("a", "three", "secret", "other")];
	sockets.forEach((item) => registry.add(item));
	registry.disconnect({ site: "a", user: "user" });
	assert.deepEqual(sockets.map((item) => item.connected), [false, false, true, true]);
});

test("client disconnect cleans up; unavailable control channel fails closed", () => {
	const registry = new DeviceSocketRegistry();
	const one = socket("a", "one");
	const two = socket("a", "two");
	registry.add(one);
	registry.add(two);
	one.disconnect(false);
	assert.equal(registry.sockets.size, 1);
	registry.disconnectAll();
	assert.equal(two.connected, false);
	assert.equal(registry.sockets.size, 0);
});

test("legacy/cookie authentication and malformed control messages are ignored", () => {
	const registry = new DeviceSocketRegistry();
	const legacy = socket("a", "key");
	legacy.authorization_header = "token key:secret";
	assert.equal(registry.add(legacy), false);
	const managed = socket("a", "key");
	registry.add(managed);
	for (const message of [null, {}, { site: "a" }, { key_hash: hash("key") }]) registry.disconnect(message);
	assert.equal(managed.connected, true);
});

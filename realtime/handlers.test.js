// Exercise the production handler with real Socket.IO server/client transports.
// Redis and Frappe HTTP verification are controlled fixtures; no site data is changed.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter, once } = require("node:events");
const { createHash } = require("node:crypto");
const { createServer } = require("node:http");
const Module = require("node:module");
const { Server } = require("../../frappe/node_modules/socket.io");
const { io } = require("../../frappe/node_modules/socket.io-client");

test("app handler enforces revocation on real sockets without client cooperation", { timeout: 10000 }, async (t) => {
	const rechecks = [];
	const originalSetInterval = global.setInterval;
	t.mock.method(global, "setInterval", (callback, delay, ...args) => {
		if (delay === 30000) rechecks.push(callback);
		return originalSetInterval(callback, delay, ...args);
	});
	const subscriber = new EventEmitter();
	let control;
	subscriber.connect = async () => subscriber.emit("ready");
	subscriber.subscribe = async (channel, callback) => {
		assert.equal(channel, "flutter_utils:realtime_control");
		control = callback;
	};
	const originalLoad = Module._load;
	let handler;
	try {
		Module._load = function (request, ...args) {
			if (request === "../../frappe/node_utils") return { get_redis_subscriber: () => subscriber };
			return originalLoad.call(this, request, ...args);
		};
		handler = require("./handlers");
	} finally {
		Module._load = originalLoad;
	}
	const server = createServer();
	const realtime = new Server(server);
	const clients = [];
	t.after(async () => {
		clients.forEach((client) => client.close());
		await new Promise((resolve) => realtime.close(resolve));
	});
	let validationAllowed = true;
	let resolveValidation;
	let validationStarted;
	for (const site of ["a", "b"]) {
		realtime.of(`/${site}`).use((socket, next) => {
			socket.user = "user";
			socket.authorization_header = socket.handshake.headers.authorization;
			socket.frappe_request = async () => {
				if (validationStarted) validationStarted();
				if (resolveValidation) await new Promise((resolve) => { resolveValidation = resolve; });
				return { ok: validationAllowed, json: async () => ({ message: { user: "user", credential: "credential" } }) };
			};
			next();
		}).on("connection", (socket) => {
			socket.join("all");
			socket.on("probe", (ack) => ack("ready"));
			handler(socket);
		});
	}
	await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
	const port = server.address().port;
	async function connect(site, authorization = "FlutterDevice key:secret", probe = true) {
		const client = io(`http://127.0.0.1:${port}/${site}`, {
			autoConnect: false, reconnection: false, transports: ["websocket"],
			extraHeaders: { Authorization: authorization },
		});
		clients.push(client);
		const connected = once(client, "connect");
		client.connect();
		await connected;
		if (probe) assert.equal(await client.timeout(2000).emitWithAck("probe"), "ready");
		return client;
	}
	const first = await connect("a");
	const otherSite = await connect("b");
	const cookie = await connect("a", "");
	// A client emitting an event with the control-channel name has no authority.
	first.emit("flutter_utils:realtime_control", { site: "a", user: "user" });
	assert.equal(await first.timeout(2000).emitWithAck("probe"), "ready");
	const disconnected = once(first, "disconnect");
	control(JSON.stringify({ site: "a", key_hash: createHash("sha256").update("key").digest("hex") }));
	assert.equal((await disconnected)[0], "io server disconnect");
	assert.equal(otherSite.connected, true);
	assert.equal(cookie.connected, true);

	// Revocation during startup disconnects the pending socket and never rejoins rooms.
	resolveValidation = () => {};
	const started = new Promise((resolve) => { validationStarted = resolve; });
	const pending = await connect("a", "FlutterDevice race:secret", false);
	await started;
	const pendingDisconnected = once(pending, "disconnect");
	control(JSON.stringify({ site: "a", key_hash: createHash("sha256").update("race").digest("hex") }));
	await pendingDisconnected;
	resolveValidation();
	resolveValidation = null;
	validationStarted = null;

	// Failure of the trusted Redis control connection closes managed sockets only.
	const lostControl = once(otherSite, "disconnect");
	subscriber.emit("error", new Error("control channel unavailable"));
	await lostControl;
	assert.equal(cookie.connected, true);

	// A periodic backend recheck catches revocation even if its message was lost.
	subscriber.emit("ready");
	const fallback = await connect("a", "FlutterDevice fallback:secret");
	validationAllowed = false;
	const staleDisconnected = once(fallback, "disconnect");
	await rechecks.at(-1)();
	await staleDisconnected;
	assert.equal(cookie.connected, true);

	// Revoked authentication is still rejected on a fresh connection.
	const revoked = await connect("a", "FlutterDevice revoked:secret", false);
	if (revoked.connected) await once(revoked, "disconnect");
	assert.equal(revoked.connected, false);
});

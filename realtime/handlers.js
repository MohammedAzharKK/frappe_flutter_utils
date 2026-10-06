const { get_redis_subscriber } = require("../../frappe/node_utils");
const { DeviceSocketRegistry } = require("./registry");

const CHANNEL = "flutter_utils:realtime_control";
const REVALIDATE_MS = 30000;
const REQUEST_TIMEOUT_MS = 10000;
const registry = new DeviceSocketRegistry();
let subscriber;
let startup;
let subscribed = false;
let ready = false;

function ensureSubscriber() {
	if (startup) return startup;
	subscriber = get_redis_subscriber();
	const failClosed = () => {
		ready = false;
		registry.disconnectAll();
	};
	subscriber.on("error", failClosed);
	subscriber.on("reconnecting", failClosed);
	subscriber.on("end", failClosed);
	subscriber.on("ready", () => { ready = subscribed; });
	startup = (async () => {
		await subscriber.connect();
		await subscriber.subscribe(CHANNEL, (raw) => {
			try {
				registry.disconnect(JSON.parse(raw));
			} catch {
				// Never log control data or socket authorization headers.
				console.warn("Invalid managed-device realtime control message");
			}
		});
		subscribed = true;
		ready = true;
	})();
	return startup;
}

async function revalidate(socket) {
	const response = await socket.frappe_request(
		"/api/method/flutter_utils.api.realtime.get_device_context",
		{},
		{ signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS) }
	);
	if (!response.ok) throw new Error("Device authentication failed");
	const { message } = await response.json();
	if (!message || message.user !== socket.user || !message.credential) {
		throw new Error("Device authentication failed");
	}
}

async function bind(socket) {
	if (!registry.add(socket)) throw new Error("Invalid managed device header");
	await ensureSubscriber();
	if (!socket.connected) return;
	if (!ready) throw new Error("Device realtime unavailable");
	// Subscribe/bind first, then reverify. A revocation during startup cannot be
	// missed between the original Frappe authentication and this binding.
	await revalidate(socket);
	if (!socket.connected) return;
	let checking = false;
	const timer = setInterval(async () => {
		if (checking || !socket.connected) return;
		checking = true;
		try {
			if (!ready) throw new Error("Device realtime unavailable");
			await revalidate(socket);
		} catch {
			socket.disconnect(false);
		} finally {
			checking = false;
		}
	}, REVALIDATE_MS);
	timer.unref();
	socket.once("disconnect", () => clearInterval(timer));
}

module.exports = (socket) => {
	if (!/^flutterdevice\b/i.test(socket.authorization_header || "")) return;
	// Frappe has already joined its default rooms. Quarantine room membership and
	// inbound packets until our control subscription and second verification finish.
	const rooms = new Set([...socket.rooms].filter((room) => room !== socket.id));
	for (const room of rooms) socket.leave(room);
	const originalJoin = socket.join;
	let active = false;
	const waiting = [];
	socket.join = (room) => {
		if (active) return originalJoin.call(socket, room);
		for (const name of Array.isArray(room) ? room : [room]) rooms.add(name);
	};
	socket.use((packet, next) => {
		if (active) return next();
		if (waiting.length >= 100) return socket.disconnect(false);
		waiting.push(next);
	});
	const startupTimeout = setTimeout(() => socket.disconnect(false), REQUEST_TIMEOUT_MS);
	startupTimeout.unref();
	socket.once("disconnect", () => {
		clearTimeout(startupTimeout);
		waiting.length = 0;
		rooms.clear();
	});
	bind(socket).then(() => {
		if (!socket.connected) return;
		clearTimeout(startupTimeout);
		active = true;
		socket.join = originalJoin;
		originalJoin.call(socket, [...rooms]);
		rooms.clear();
		for (const next of waiting.splice(0)) next();
	}).catch(() => socket.disconnect(false));
};

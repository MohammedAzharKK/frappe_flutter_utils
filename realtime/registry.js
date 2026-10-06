const { createHash } = require("node:crypto");

const hash = (value) => createHash("sha256").update(value).digest("hex");

class DeviceSocketRegistry {
	constructor() {
		this.sockets = new Map();
	}

	add(socket) {
		const match = /^FlutterDevice ([^: ]+):([^: ]+)$/i.exec(socket.authorization_header || "");
		if (!match) return false;
		this.sockets.set(socket, {
			site: socket.nsp.name.slice(1),
			user: socket.user,
			key_hash: hash(match[1]),
			generation: hash(`${match[1]}:${match[2]}`),
		});
		socket.once("disconnect", () => this.sockets.delete(socket));
		return true;
	}

	disconnect(message) {
		if (!message || typeof message.site !== "string") return;
		if (typeof message.key_hash !== "string" && typeof message.user !== "string") return;
		for (const [socket, identity] of this.sockets) {
			if (identity.site !== message.site) continue;
			if (message.key_hash && identity.key_hash !== message.key_hash) continue;
			if (message.generation && identity.generation !== message.generation) continue;
			if (message.user && identity.user !== message.user) continue;
			this.sockets.delete(socket);
			// Disconnect this namespace server-side; don't rely on client cooperation.
			socket.disconnect(false);
		}
	}

	disconnectAll() {
		for (const socket of this.sockets.keys()) socket.disconnect(false);
		this.sockets.clear();
	}
}

module.exports = { DeviceSocketRegistry };

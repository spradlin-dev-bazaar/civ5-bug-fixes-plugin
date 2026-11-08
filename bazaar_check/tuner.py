"""A client for Civilization V's tuner port, the one FireTuner uses: it lists the game's Lua states and runs Lua in
one of them. The game opens the port when its config.ini has EnableTuner = 1, and sends back what Lua prints only with
LoggingEnabled = 1. It serves one client at a time.

Each message is a 32-bit length and a 32-bit listener number, little-endian, then text fields that each end in a zero
byte. "LSQ:" asks for the Lua states, which come back as id and name pairs, sometimes after an "L" field.
"CMD:<state id>:<Lua>" runs Lua; each print comes back as an "O" field, then "<state name>: <text>". A command the
game refuses comes back as "ERR:".
"""

import socket
import struct
import time
import uuid

HOST = "127.0.0.1"
PORT = 4318
STATE_LIST = 1
COMMANDS = 2
# The game answers a command over about 2 KB with an empty error, so longer Lua is sent in pieces.
MAX_PIECE = 1500
BUFFER = "_BazaarTunerCode"
ERROR_TAG = "__bazaar_lua_error__"
WRAPPER = (
    "print('%(start)s') "
    "local f, e = loadstring(%(code)s) "
    "if not f then print('%(tag)s ' .. tostring(e)) "
    "else local ok, r = pcall(f) if not ok then print('%(tag)s ' .. tostring(r)) end end "
    "%(buffer)s = nil print('%(end)s')"
)


class TunerError(Exception):
    pass


class NoSuchState(TunerError):
    pass


class LuaError(Exception):
    pass


def frame(text, listener):
    payload = text.encode("utf-8") + b"\0"
    return struct.pack("<II", len(payload), listener) + payload


def parse_states(fields):
    if fields and fields[0] == "L":
        fields = fields[1:]
    if not fields or not fields[0].isdigit():
        return None
    return [(int(fields[i]), fields[i + 1]) for i in range(0, len(fields) - 1, 2)]


def lua_escape(char):
    if char in '\\"':
        return "\\" + char
    if char == "\n":
        return "\\n"
    if ord(char) < 32:
        return "\\%03d" % ord(char)
    return char


def lua_text(text):
    return '"%s"' % "".join(lua_escape(c) for c in text)


def lua_strings(text, limit):
    """Lua string literals of at most limit bytes each, since the game's limit is in bytes."""
    pieces, current, size = [], "", 2
    for char in text:
        escaped = lua_escape(char)
        width = len(escaped.encode("utf-8"))
        if size + width > limit:
            pieces.append('"%s"' % current)
            current, size = "", 2
        current += escaped
        size += width
    pieces.append('"%s"' % current)
    return pieces


class Tuner:
    def __init__(self, host=HOST, port=PORT):
        self.address = (host, port)
        self.sock = None
        self.received = b""

    def connect(self, timeout):
        """Retries for timeout seconds, because the game may refuse a client right after the last one left."""
        deadline = time.time() + timeout
        while True:
            self.close()
            try:
                self.sock = socket.create_connection(self.address, timeout=5)
                # The game does not notice a socket that closes normally, so closing resets it instead.
                self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                self.states()
                return
            except (OSError, TunerError) as e:
                if time.time() > deadline:
                    self.close()
                    raise TunerError(
                        "no answer on the tuner port %s:%d (%s): is Civilization V running, with EnableTuner = 1 in"
                        " its config.ini, and FireTuner closed?" % (self.address + (e,))
                    )
                time.sleep(2)

    @property
    def connected(self):
        return self.sock is not None

    def close(self):
        if self.sock is not None:
            self.sock.close()
            self.sock = None
        self.received = b""

    def _send(self, text, listener):
        if self.sock is None:
            raise TunerError("not connected to the tuner port")
        try:
            self.sock.settimeout(10)
            self.sock.sendall(frame(text, listener))
        except OSError as e:
            raise TunerError("the tuner connection failed: %s" % e)

    def _read(self, timeout):
        """The next message as (listener, fields), or None after timeout seconds. Part of a message that has arrived
        stays in self.received for the next call."""
        deadline = time.time() + timeout
        while True:
            if len(self.received) >= 8:
                size, listener = struct.unpack("<II", self.received[:8])
                if len(self.received) >= 8 + size:
                    payload, self.received = self.received[8 : 8 + size], self.received[8 + size :]
                    return listener, [f.decode("utf-8", "replace") for f in payload.rstrip(b"\0").split(b"\0")]
            remaining = deadline - time.time()
            if remaining <= 0:
                return None
            if self.sock is None:
                raise TunerError("not connected to the tuner port")
            self.sock.settimeout(remaining)
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                return None
            except OSError as e:
                raise TunerError("the tuner connection failed: %s" % e)
            if not chunk:
                raise TunerError("the game closed the tuner connection")
            self.received += chunk

    def states(self, timeout=10):
        self._send("LSQ:", STATE_LIST)
        deadline = time.time() + timeout
        while True:
            message = self._read(deadline - time.time())
            if message is None:
                raise TunerError("the game did not send its list of Lua states")
            listener, fields = message
            found = parse_states(fields) if listener == STATE_LIST else None
            if found is not None:
                return found

    def state_ids(self, name):
        return [sid for sid, n in self.states() if n == name]

    def state_id(self, name):
        """The newest state of that name, since a loaded game makes its states again with higher ids."""
        ids = self.state_ids(name)
        if not ids:
            raise NoSuchState("the game has no Lua state named %s" % name)
        return max(ids)

    def run(self, state, code, timeout=30):
        """The lines code printed in the named Lua state; what it returns is lost. A Lua error raises LuaError."""
        sid = self.state_id(state)
        tag = uuid.uuid4().hex
        start, end = "__start_%s__" % tag, "__done_%s__" % tag
        wrapped = WRAPPER % {
            "start": start,
            "code": self._stage(sid, code),
            "tag": ERROR_TAG,
            "buffer": BUFFER,
            "end": end,
        }
        self._send("CMD:%d:%s" % (sid, wrapped), COMMANDS)
        return self._collect(state, start, end, timeout)

    def _stage(self, sid, code):
        pieces = lua_strings(code, MAX_PIECE)
        if len(pieces) == 1:
            return pieces[0]
        for i, piece in enumerate(pieces):
            self._send("CMD:%d:%s = %s%s" % (sid, BUFFER, BUFFER + " .. " if i else "", piece), COMMANDS)
        return BUFFER

    def _collect(self, state, start, end, timeout):
        """Only lines between the command's own markers count: other scripts in the state print too, and an earlier
        command that timed out may still answer."""
        prefix = state + ": "
        lines, errors, started = [], [], False
        deadline = time.time() + timeout
        while True:
            message = self._read(deadline - time.time())
            if message is None:
                raise TunerError(
                    "no answer from the %s Lua state within %d seconds: the game may be busy, or LoggingEnabled"
                    " may not be 1 in its config.ini" % (state, timeout)
                )
            _, fields = message
            if fields and fields[0].startswith("ERR:"):
                raise LuaError("the game refused a command in %s: %s" % (state, "\n".join(fields)[4:].strip()))
            text = "\n".join(fields[1:]) if fields and fields[0] == "O" else "\n".join(fields)
            if not text.startswith(prefix):
                continue
            body = text[len(prefix) :]
            if body.strip() == start:
                started = True
            elif not started:
                continue
            elif body.strip() == end:
                if errors:
                    raise LuaError("Lua error in %s: %s" % (state, "\n".join(errors)))
                return lines
            elif body.startswith(ERROR_TAG):
                errors.append(body[len(ERROR_TAG) :].strip())
            else:
                lines.extend(body.split("\n"))

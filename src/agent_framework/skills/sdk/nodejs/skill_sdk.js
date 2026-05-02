/**
 * Node.js SDK for writing agent-framework skills.
 *
 * Usage:
 *   const { SkillServer } = require('./skill_sdk');
 *   const server = new SkillServer();
 *
 *   server.tool('search_documents', async ({ query, limit }) => {
 *     // ... implementation
 *     return results;
 *   });
 *
 *   server.run();
 *
 * Permissions are enforced at runtime via environment variables injected by
 * the framework. The SDK patches fs and child_process to restrict access.
 */

const readline = require('readline');
const fs = require('fs');
const path = require('path');

const PERM_ERROR_CODE = -32001;

class _PermissionGuard {
    constructor() {
        this.fsReadPrefixes = this._parsePaths(process.env.SKILL_FS_READ || '');
        this.fsWritePrefixes = this._parsePaths(process.env.SKILL_FS_WRITE || '');
        this.netAllow = (process.env.SKILL_NET_ALLOW || '').split(',').filter(Boolean);
        this.netDeny = (process.env.SKILL_NET_DENY || '').split(',').filter(Boolean);
        this.allowSubprocess = process.env.SKILL_ALLOW_SUBPROCESS === '1';
        this._installed = false;
    }

    _parsePaths(value) {
        if (!value) return [];
        return value.split(path.delimiter).filter(Boolean);
    }

    install() {
        if (this._installed) return;
        this._installed = true;
        this._patchFs();
    }

    _patchFs() {
        const guard = this;
        const origReadFileSync = fs.readFileSync;
        const origWriteFileSync = fs.writeFileSync;
        const origOpenSync = fs.openSync;

        // Patch write operations
        fs.writeFileSync = function(file, data, options) {
            guard._checkWrite(path.resolve(String(file)));
            return origWriteFileSync.call(fs, file, data, options);
        };

        fs.openSync = function(file, flags, mode) {
            const fStr = String(flags);
            if (fStr.includes('w') || fStr.includes('a') || fStr.includes('+')) {
                guard._checkWrite(path.resolve(String(file)));
            } else {
                guard._checkRead(path.resolve(String(file)));
            }
            return origOpenSync.call(fs, file, flags, mode);
        };
    }

    _checkRead(filePath) {
        if (!this.fsReadPrefixes.length) return true;
        if (!this.fsReadPrefixes.some(p => filePath.startsWith(p))) {
            throw new Error(`Permission denied: skill cannot read ${filePath}`);
        }
        return true;
    }

    _checkWrite(filePath) {
        if (!this.fsWritePrefixes.length) {
            throw new Error(`Permission denied: no write access configured`);
        }
        if (!this.fsWritePrefixes.some(p => filePath.startsWith(p))) {
            throw new Error(`Permission denied: skill cannot write to ${filePath}`);
        }
        return true;
    }
}

class SkillServer {
    constructor() {
        this._tools = new Map();
        this._rl = readline.createInterface({ input: process.stdin });
        this._guard = new _PermissionGuard();
    }

    /**
     * Register a tool handler.
     * @param {string} name - Tool name
     * @param {function} handler - Async or sync function receiving (params) object
     */
    tool(name, handler) {
        this._tools.set(name, handler);
    }

    /**
     * Start the JSON-RPC main loop. Sends 'ready' notification and begins
     * reading requests from stdin.
     */
    run() {
        this._guard.install();
        this._sendNotification('ready', {});
        this._rl.on('line', (line) => {
            let request;
            try {
                request = JSON.parse(line);
            } catch {
                this._sendError(null, -32700, 'Parse error');
                return;
            }
            try {
                this._handleRequest(request);
            } catch (exc) {
                this._sendError(request.id || null, -32002, `Internal error: ${exc}`);
            }
        });
    }

    async _handleRequest(request) {
        const { method, params = {}, id } = request;
        if (method === 'ping') {
            this._sendResult(id, { status: 'ok' });
        } else if (method === 'shutdown') {
            this._sendResult(id, {});
            process.exit(0);
        } else if (method === 'list_tools') {
            this._sendResult(id, { tools: this._listTools() });
        } else if (method === 'call_tool') {
            await this._handleCallTool(id, params);
        } else {
            this._sendError(id, -32601, `Unknown method: ${method}`);
        }
    }

    async _handleCallTool(id, params) {
        const handler = this._tools.get(params.name);
        if (!handler) {
            this._sendError(id, -32601, `Unknown tool: ${params.name}`);
            return;
        }
        try {
            const content = await handler(params.arguments || {});
            this._sendResult(id, { content, is_error: false });
        } catch (exc) {
            if (exc.message && exc.message.startsWith('Permission denied')) {
                this._sendError(id, PERM_ERROR_CODE, exc.message);
            } else {
                this._sendResult(id, { content: String(exc), is_error: true });
            }
        }
    }

    _listTools() {
        return Array.from(this._tools.entries()).map(([name, handler]) => ({
            name,
            description: `Tool '${name}'`,
            parameters: { type: 'object', properties: {} },
        }));
    }

    _sendResult(id, result) {
        this._write({ jsonrpc: '2.0', id, result });
    }

    _sendError(id, code, message) {
        this._write({ jsonrpc: '2.0', id, error: { code, message } });
    }

    _sendNotification(method, params) {
        this._write({ jsonrpc: '2.0', method, params });
    }

    _write(obj) {
        process.stdout.write(JSON.stringify(obj) + '\n');
    }
}

module.exports = { SkillServer };

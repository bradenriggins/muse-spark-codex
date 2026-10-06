#!/usr/bin/env python3
"""Muse-Codex translation shim.

Codex speaks OpenAI Responses API with extensions Meta rejects:
  * `custom` tools (apply_patch freeform) -> translated to plain functions.
    Model replies come back as function_call and are rewritten to
    custom_tool_call (streaming-aware), so Codex still runs its native
    freeform patch flow. Follow-up replays are translated back.
  * `web_search` -> translated to bare `web_search_preview` (accepted).
  * nested functions with recursive $ref schemas -> stripped (Meta rejects
    recursion; e.g. Gmail MIME-part schemas). Everything else passes through,
    including `namespace` tools.
  * all other bytes (SSE text/reasoning deltas, ids, usage) pass through.

Runs on 127.0.0.1 only. Forwards the client's Authorization header untouched;
logs tool names and byte counts, never prompts or keys.
"""

import http.server
import json
import logging
import os
import threading
import urllib.request

UPSTREAM = "https://api.meta.ai"
BIND = ("127.0.0.1", 18199)
LOG_PATH = os.path.expanduser("~/Library/Logs/muse-codex-shim.log")

logging.basicConfig(
    filename=LOG_PATH,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

# (namespace, name) pairs converted custom->function per request. Namespaces
# are None for top-level tools.
_CONVERTED_KIND = "muse_shim_converted_custom"


def _walk_refs(node, local_refs):
    """Collect local $ref targets (e.g. '#/$defs/X') under node."""
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/"):
            local_refs.append(ref)
        for value in node.values():
            _walk_refs(value, local_refs)
    elif isinstance(node, list):
        for value in node:
            _walk_refs(value, local_refs)


def _schema_has_ref_cycle(schema):
    """True when a local $ref cycle is reachable from the schema root."""
    if not isinstance(schema, dict):
        return False
    defs = schema.get("$defs", {})
    if not isinstance(defs, dict):
        defs = {}

    def targets_of(node):
        refs = []
        _walk_refs(node, refs)
        return refs

    # Graph nodes: "#root" plus "#/$defs/<name>".
    root_refs = targets_of({k: v for k, v in schema.items() if k != "$defs"})
    adjacency = {"#root": [r for r in root_refs if r == "#root" or r.startswith("#/$defs/")]}
    for name, subschema in defs.items():
        key = f"#/$defs/{name}"
        adjacency[key] = [
            r for r in targets_of(subschema)
            if r == "#root" or r.startswith("#/$defs/")
        ]
    # Cycle reachable from root?
    visited = set()
    stack = set()

    def visit(node):
        if node in stack:
            return True
        if node in visited:
            return False
        visited.add(node)
        stack.add(node)
        for nxt in adjacency.get(node, []):
            if visit(nxt):
                return True
        stack.discard(node)
        return False

    return visit("#root")


def _custom_to_function(tool):
    """Convert a `custom` tool to a plain function tool (input <-> input)."""
    description = tool.get("description", "")
    fmt = tool.get("format") or {}
    grammar = fmt.get("definition") if isinstance(fmt, dict) else None
    extra = (
        "\n\nPut the entire tool input in the `input` string. "
        "Emit raw input text exactly as specified (no JSON wrapper, "
        "no markdown fences)."
    )
    if grammar:
        extra += f"\n\nInput grammar ({fmt.get('syntax', 'lark')}):\n{grammar}"
    return {
        "type": "function",
        "name": tool.get("name"),
        "description": description + extra,
        "strict": False,
        "parameters": {
            "type": "object",
            "properties": {
                "input": {
                    "type": "string",
                    "description": "Raw tool input text.",
                }
            },
            "required": ["input"],
            "additionalProperties": False,
        },
    }


def _translate_tools_out(tools):
    """Codex -> Meta. Returns (new_tools, converted, namespaces, functions).

    converted: {(namespace, name)} custom->function conversions.
    namespaces: {namespace: {nested function names}} for call fixup.
    functions: {top-level function names} (bare-name guard).
    """
    converted = set()
    namespaces = {}
    functions = set()
    stripped = []
    out = []
    for tool in tools or []:
        ttype = tool.get("type")
        if ttype == "custom":
            out.append(_custom_to_function(tool))
            converted.add((None, tool.get("name")))
        elif ttype == "web_search":
            out.append({"type": "web_search_preview"})
        elif ttype == "namespace":
            nested = []
            nested_names = set()
            for sub in tool.get("tools") or []:
                if sub.get("type") == "custom":
                    nested.append(_custom_to_function(sub))
                    converted.add((tool.get("name"), sub.get("name")))
                    nested_names.add(sub.get("name"))
                elif _schema_has_ref_cycle(sub.get("parameters")):
                    stripped.append(f"{tool.get('name')}.{sub.get('name')}")
                else:
                    nested.append(sub)
                    nested_names.add(sub.get("name"))
            namespaces[tool.get("name")] = nested_names
            out.append({**tool, "tools": nested})
        elif ttype == "function" and _schema_has_ref_cycle(tool.get("parameters")):
            stripped.append(tool.get("name"))
        else:
            out.append(tool)
            if ttype == "function" and isinstance(tool.get("name"), str):
                functions.add(tool.get("name"))
    functions.update(n for _, n in converted if isinstance(n, str))
    if stripped:
        logging.info("stripped recursive-schema tools: %s", ",".join(stripped))
    if converted:
        logging.info("custom->function: %s", sorted(n for _, n in converted))
    return out, converted, namespaces, functions


def _repair_arguments(item):
    """Meta sometimes emits a function_call with empty/invalid arguments, then
    rejects its own history with 400 on the next turn. Normalize to '{}' so the
    tool errors gracefully instead of killing the session. Returns True if repaired."""
    if not isinstance(item, dict) or item.get("type") != "function_call":
        return False
    args = item.get("arguments")
    if isinstance(args, str):
        try:
            json.loads(args)
            return False
        except ValueError:
            pass
    item["arguments"] = "{}"
    return True


def _split_namespaced_call(item, namespaces, functions):
    """Meta emits namespaced calls flat ('ns.tool'); Codex wants the split
    form. Meta also sometimes drops the prefix entirely ('tool'); resolve
    a bare name only when it matches exactly one namespaced tool and no
    top-level function, otherwise leave it for the router."""
    if (
        not isinstance(item, dict)
        or item.get("type") != "function_call"
        or item.get("namespace")
        or not isinstance(item.get("name"), str)
    ):
        return item
    name = item["name"]
    if "." in name:
        prefix, _, suffix = name.partition(".")
        if prefix in namespaces and suffix in namespaces[prefix]:
            item = {**item, "namespace": prefix, "name": suffix}
        return item
    if name in functions:
        return item
    hits = [ns for ns, tools in namespaces.items() if name in tools]
    if len(hits) == 1:
        logging.info("resolved bare call %s -> %s.%s", name, hits[0], name)
        item = {**item, "namespace": hits[0]}
    return item


def _translate_items_out(items):
    """Rewrite replayed custom items to function items (follow-up turns)."""
    out = []
    for item in items or []:
        if not isinstance(item, dict):
            out.append(item)
            continue
        itype = item.get("type")
        if itype == "custom_tool_call":
            out.append({
                "type": "function_call",
                "id": item.get("id"),
                "call_id": item.get("call_id"),
                "name": item.get("name"),
                "namespace": item.get("namespace"),
                "arguments": json.dumps({"input": item.get("input", "")}),
            })
        elif itype == "custom_tool_call_output":
            out.append({
                "type": "function_call_output",
                "id": item.get("id"),
                "call_id": item.get("call_id"),
                "output": item.get("output"),
            })
        elif itype == "agent_message":
            # Multi-agent handoff (Meta has no such type; it never emits
            # these, so one-way translation is enough). Codex keeps the
            # original locally and resends it every turn.
            parts = []
            for c in (item.get("content") or []):
                if not isinstance(c, dict):
                    continue
                # NEW_TASK carries the assignment in an encrypted_content
                # part (plaintext locally); FINAL_ANSWER uses input_text.
                for k in ("text", "encrypted_content"):
                    if isinstance(c.get(k), str):
                        parts.append(c[k])
            header = "[agent %s -> %s]" % (item.get("author"),
                                           item.get("recipient"))
            text = header + "\n" + "\n".join(parts) if parts else header
            out.append({
                "type": "message",
                "id": item.get("id"),
                "role": "user",
                "content": [{"type": "input_text", "text": text}],
            })
        else:
            out.append(item)
    return out


def _function_call_to_custom(item):
    """Rewrite one completed function_call item back to custom_tool_call."""
    try:
        args = json.loads(item.get("arguments") or "")
        text = args.get("input") if isinstance(args, dict) and "input" in args else item.get("arguments")
    except (ValueError, TypeError):
        text = item.get("arguments")
    return {
        "type": "custom_tool_call",
        "id": item.get("id"),
        "call_id": item.get("call_id"),
        "name": item.get("name"),
        "namespace": item.get("namespace"),
        "input": text if isinstance(text, str) else json.dumps(text),
    }


def _is_converted_call(item, converted):
    return (
        isinstance(item, dict)
        and item.get("type") == "function_call"
        and (item.get("namespace"), item.get("name")) in converted
    )


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _send_json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/healthz":
            self._send_json(200, {"ok": True})
            return
        if self.path == "/v1/models" or self.path.startswith("/v1/models?"):
            self._forward("GET", None)
            return
        self._send_json(404, {"error": "unknown path"})

    def do_POST(self):
        if self.path != "/v1/responses":
            self._send_json(404, {"error": "unknown path"})
            return
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(body) if body else {}
        except ValueError:
            self._send_json(400, {"error": "invalid JSON"})
            return
        if isinstance(payload.get("input"), list):
            native = {}
            for _item in payload["input"]:
                _t = _item.get("type") if isinstance(_item, dict) else "?"
                native[_t] = native.get(_t, 0) + 1
            logging.info("input types: %s",
                         ",".join("%s x%d" % kv for kv in sorted(native.items())))
            payload["input"] = _translate_items_out(payload["input"])
            repaired = [str(i.get("name")) for i in payload["input"]
                        if _repair_arguments(i)]
            if repaired:
                logging.info("repaired invalid input function_call arguments: %s",
                             ",".join(repaired))
        ctx = {"converted": set(), "namespaces": {}, "functions": set()}
        if isinstance(payload.get("tools"), list):
            payload["tools"], ctx["converted"], ctx["namespaces"], ctx["functions"] = _translate_tools_out(
                payload["tools"]
            )
        logging.info(
            "request model=%s stream=%s tools=%d input_items=%d",
            payload.get("model"), payload.get("stream"),
            len(payload.get("tools") or []),
            len(payload.get("input") or []) if isinstance(payload.get("input"), list) else -1,
        )
        self._forward("POST", (json.dumps(payload).encode(), ctx))

    def _forward(self, method, post_data):
        headers = {"Authorization": self.headers.get("Authorization", "")}
        data = None
        ctx = {"converted": set(), "namespaces": {}, "functions": set()}
        if post_data is not None:
            data, ctx = post_data
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(UPSTREAM + self.path, data=data,
                                     headers=headers, method=method)
        try:
            upstream = urllib.request.urlopen(req, timeout=None)
        except urllib.error.HTTPError as err:
            raw = err.read()
            logging.info("upstream HTTP %s: %s", err.code, raw[:300])
            self.send_response(err.code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        except Exception as err:  # network down, DNS, ...
            logging.warning("upstream failed: %r", err)
            self._send_json(502, {"error": f"upstream failed: {err}"})
            return
        ctype = upstream.headers.get("Content-Type", "")
        if "text/event-stream" in ctype:
            self._relay_stream(upstream, ctx)
        else:
            raw = upstream.read()
            raw = self._translate_full_body(raw, ctx)
            self.send_response(upstream.status)
            self.send_header("Content-Type", ctype or "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    def _translate_full_body(self, raw, ctx):
        converted = ctx["converted"]
        namespaces = ctx["namespaces"]
        functions = ctx["functions"]
        try:
            obj = json.loads(raw)
        except ValueError:
            return raw
        output = (obj.get("response") or obj).get("output") if isinstance(obj, dict) else None
        if isinstance(output, list):
            for i, item in enumerate(output):
                item = _split_namespaced_call(item, namespaces, functions)
                if _is_converted_call(item, converted):
                    item = _function_call_to_custom(item)
                elif _repair_arguments(item):
                    logging.info("repaired invalid output function_call arguments: %s",
                                 item.get("name"))
                output[i] = item
        return json.dumps(obj).encode()

    def _relay_stream(self, upstream, ctx):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        # item_id -> True while buffering a converted call's partial events.
        buffering = {}
        event_lines = []

        def flush_event():
            if not event_lines:
                return
            name = None
            data_lines = []
            for line in event_lines:
                if line.startswith("event:"):
                    name = line[len("event:"):].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[len("data:"):].lstrip())
                # ':' comments fall through untouched below
            raw_data = "\n".join(data_lines)
            if name and raw_data and name.startswith("response."):
                try:
                    data = json.loads(raw_data)
                except ValueError:
                    data = None
                if isinstance(data, dict):
                    rewritten = self._rewrite_event(name, data, ctx, buffering)
                    if rewritten is None:
                        event_lines.clear()
                        return  # swallowed (buffered partial)
                    prefix_events = []
                    if isinstance(rewritten, dict) and rewritten.pop("_shim_emit_added_first", False):
                        prefix_events.append(("response.output_item.added", {
                            "output_index": rewritten.get("output_index", 0),
                            "item": rewritten["item"],
                        }))
                    raw_data = json.dumps(rewritten)
                    event_lines[:] = [l for l in event_lines if not l.startswith("data:")]
                    event_lines.append(f"data: {raw_data}")
                    for prefix_name, prefix_data in prefix_events:
                        self.wfile.write(f"event: {prefix_name}\n".encode())
                        self.wfile.write(f"data: {json.dumps(prefix_data)}\n\n".encode())
                    self.wfile.flush()
            for line in event_lines:
                self.wfile.write(line.encode() + b"\n")
            self.wfile.write(b"\n")
            self.wfile.flush()
            event_lines.clear()

        while True:
            line = upstream.readline()
            if not line:
                break
            text = line.decode("utf-8", "replace").rstrip("\r\n")
            if text == "":
                flush_event()
            else:
                event_lines.append(text)
        flush_event()

    def _rewrite_event(self, name, data, ctx, buffering):
        converted = ctx["converted"]
        namespaces = ctx["namespaces"]
        functions = ctx["functions"]
        if name == "response.output_item.added":
            item = _split_namespaced_call(data.get("item") or {}, namespaces, functions)
            data["item"] = item
            if _is_converted_call(item, converted):
                buffering[item.get("id")] = data.get("output_index")
                return None  # hold until done; deltas are JSON fragments
            return data
        if name in ("response.function_call_arguments.delta",
                    "response.custom_tool_call_input.delta"):
            if data.get("item_id") in buffering:
                return None
            return data
        if name == "response.output_item.done":
            item = _split_namespaced_call(data.get("item") or {}, namespaces, functions)
            if _is_converted_call(item, converted):
                data["item"] = _function_call_to_custom(item)
                data["_shim_emit_added_first"] = True
                buffering.pop(item.get("id"), None)
                return data
            if _repair_arguments(item):
                logging.info("repaired invalid output function_call arguments: %s",
                             item.get("name"))
            data["item"] = item
            return data
        if name in ("response.completed", "response.incomplete", "response.failed"):
            response = data.get("response") or {}
            output = response.get("output")
            if isinstance(output, list):
                for i, item in enumerate(output):
                    item = _split_namespaced_call(item, namespaces, functions)
                    if _is_converted_call(item, converted):
                        item = _function_call_to_custom(item)
                    elif _repair_arguments(item):
                        logging.info("repaired invalid output function_call arguments: %s",
                                     item.get("name"))
                    output[i] = item
            return data
        return data


def main():
    server = http.server.ThreadingHTTPServer(BIND, Handler)
    server.daemon_threads = True
    logging.info("muse-codex-shim listening on %s:%d", *BIND)
    server.serve_forever()


if __name__ == "__main__":
    main()

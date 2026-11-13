"""Kubernetes MCP server for Hermes.

Runs as a stdio MCP server inside the Hermes pod and talks to the cluster with
the pod's own ServiceAccount. The credential never leaves the pod, so an agent
command in the sandbox can neither see it nor reach the API server.

Approval is Hermes' job, not this server's: tools that read are annotated
``readOnlyHint: true`` and run freely, and tools that change state are left
unannotated so the ``trust: untrusted`` gate in Hermes asks the user in Signal
before each call. The one deliberate exception is secret reads, which are reads
but are annotated like a write so they also ask.

No shell: arguments are passed as an argv list, never through ``sh -c``, so a
resource name containing shell metacharacters is just a bad argument.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

SERVER_NAME = "kube"
SERVER_VERSION = "0.1.0"
DEFAULT_PROTOCOL = "2024-11-05"

#: Keep tool output inside a size a chat message can carry.
MAX_OUTPUT_BYTES = 40_000
TIMEOUT_SECONDS = 60


def _build_kubeconfig() -> str | None:
    """Write a kubeconfig from the in-cluster ServiceAccount, once."""
    path = "/tmp/kubeconfig-hermes.yaml"
    if os.path.exists(path):
        return path
    host, port = os.environ.get("KUBERNETES_SERVICE_HOST"), os.environ.get("KUBERNETES_SERVICE_PORT")
    token_path = "/var/run/secrets/kubernetes.io/serviceaccount/token"
    ca_path = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
    if not (host and port and os.path.exists(token_path)):
        return None
    with open(token_path, encoding="utf-8") as handle:
        token = handle.read().strip()
    body = [
        "apiVersion: v1",
        "kind: Config",
        "clusters:",
        "- name: in-cluster",
        "  cluster:",
        f"    server: https://{host}:{port}",
    ]
    if os.path.exists(ca_path):
        body.append(f"    certificate-authority: {ca_path}")
    body += ["contexts:", "- name: hermes", "  context:", "    cluster: in-cluster", "    user: hermes",
             "current-context: hermes", "users:", "- name: hermes", "  user:", f"    token: {token}"]
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(body) + "\n")
    os.chmod(path, 0o600)
    return path


def run_kubectl(args: list[str]) -> tuple[bool, str]:
    kubeconfig = _build_kubeconfig()
    if kubeconfig is None:
        return False, "no in-cluster ServiceAccount token; check automountServiceAccountToken"
    argv = ["kubectl", f"--kubeconfig={kubeconfig}", *args]
    try:
        completed = subprocess.run(
            argv, capture_output=True, text=True, timeout=TIMEOUT_SECONDS, check=False
        )
    except subprocess.TimeoutExpired:
        return False, f"kubectl timed out after {TIMEOUT_SECONDS}s: {' '.join(args)}"
    except OSError as exc:
        return False, f"could not run kubectl: {exc}"
    output = (completed.stdout or "") + (completed.stderr or "")
    if len(output.encode()) > MAX_OUTPUT_BYTES:
        head = output.encode()[: MAX_OUTPUT_BYTES // 2]
        tail = output.encode()[-MAX_OUTPUT_BYTES // 2 :]
        output = head.decode(errors="replace") + "\n…[truncated]…\n" + tail.decode(errors="replace")
    return completed.returncode == 0, output.rstrip() or "(no output)"


def _ns(value: object) -> list[str]:
    text = str(value or "").strip()
    return [f"--namespace={text}"] if text else []


def tool_get(args: dict) -> tuple[bool, str]:
    resource = str(args.get("resource") or "").strip()
    if not resource:
        return False, "resource is required, e.g. pods, deployments, nodes"
    argv = ["get", resource, *(_ns(args.get("namespace")))]
    if args.get("name"):
        argv.append(str(args["name"]))
    if args.get("selector"):
        argv.append(f"--selector={args['selector']}")
    if args.get("all_namespaces"):
        argv.append("--all-namespaces")
    argv += ["-o", str(args.get("output") or "wide")]
    return run_kubectl(argv)


def tool_describe(args: dict) -> tuple[bool, str]:
    resource, name = str(args.get("resource") or "").strip(), str(args.get("name") or "").strip()
    if not resource or not name:
        return False, "resource and name are required"
    return run_kubectl(["describe", resource, name, *_ns(args.get("namespace"))])


def tool_logs(args: dict) -> tuple[bool, str]:
    pod = str(args.get("pod") or "").strip()
    if not pod:
        return False, "pod is required"
    argv = ["logs", pod, *_ns(args.get("namespace"))]
    if args.get("container"):
        argv.append(f"--container={args['container']}")
    argv.append(f"--tail={int(args.get('tail') or 100)}")
    if args.get("previous"):
        argv.append("--previous")
    return run_kubectl(argv)


def tool_events(args: dict) -> tuple[bool, str]:
    argv = ["get", "events", "--sort-by=.lastTimestamp"]
    argv += _ns(args.get("namespace")) or ["--all-namespaces"]
    return run_kubectl(argv + ["--tail=60"]) if False else run_kubectl(argv)


def tool_apply(args: dict) -> tuple[bool, str]:
    manifest = str(args.get("manifest") or "").strip()
    if not manifest:
        return False, "manifest (YAML text) is required"
    kubeconfig = _build_kubeconfig()
    if kubeconfig is None:
        return False, "no in-cluster ServiceAccount token"
    try:
        completed = subprocess.run(
            ["kubectl", f"--kubeconfig={kubeconfig}", "apply", "-f", "-", *_ns(args.get("namespace"))],
            input=manifest, capture_output=True, text=True, timeout=TIMEOUT_SECONDS, check=False,
        )
    except subprocess.TimeoutExpired:
        return False, "kubectl apply timed out"
    return completed.returncode == 0, ((completed.stdout or "") + (completed.stderr or "")).strip() or "(no output)"


def tool_delete(args: dict) -> tuple[bool, str]:
    resource, name = str(args.get("resource") or "").strip(), str(args.get("name") or "").strip()
    if not resource or not name:
        return False, "resource and name are required; refusing a delete without both"
    return run_kubectl(["delete", resource, name, *_ns(args.get("namespace")), "--wait=false"])


def tool_scale(args: dict) -> tuple[bool, str]:
    resource, name = str(args.get("resource") or "deployment").strip(), str(args.get("name") or "").strip()
    if not name or args.get("replicas") is None:
        return False, "name and replicas are required"
    return run_kubectl(["scale", resource, name, f"--replicas={int(args['replicas'])}", *_ns(args.get("namespace"))])


def tool_rollout(args: dict) -> tuple[bool, str]:
    action = str(args.get("action") or "restart").strip()
    if action not in ("restart", "status", "undo"):
        return False, "action must be restart, status or undo"
    resource, name = str(args.get("resource") or "deployment").strip(), str(args.get("name") or "").strip()
    if not name:
        return False, "name is required"
    return run_kubectl(["rollout", action, f"{resource}/{name}", *_ns(args.get("namespace"))])


def tool_secret_get(args: dict) -> tuple[bool, str]:
    name = str(args.get("name") or "").strip()
    if not name:
        return False, "name is required"
    return run_kubectl(["get", "secret", name, *_ns(args.get("namespace")), "-o", "yaml"])


READ = {"readOnlyHint": True, "destructiveHint": False}
WRITE = {"readOnlyHint": False, "destructiveHint": True}

TOOLS = [
    {
        "name": "kube_get",
        "description": "List or read any Kubernetes resource. Use output=json for the full object.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "resource": {"type": "string", "description": "pods, deployments, nodes, pvc, ..."},
                "name": {"type": "string"},
                "namespace": {"type": "string"},
                "all_namespaces": {"type": "boolean"},
                "selector": {"type": "string"},
                "output": {"type": "string", "description": "wide (default), json, yaml, name"},
            },
            "required": ["resource"],
        },
        "annotations": READ,
    },
    {
        "name": "kube_describe",
        "description": "Describe one resource, including its events.",
        "inputSchema": {
            "type": "object",
            "properties": {"resource": {"type": "string"}, "name": {"type": "string"}, "namespace": {"type": "string"}},
            "required": ["resource", "name"],
        },
        "annotations": READ,
    },
    {
        "name": "kube_logs",
        "description": "Read pod logs.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pod": {"type": "string"},
                "namespace": {"type": "string"},
                "container": {"type": "string"},
                "tail": {"type": "integer"},
                "previous": {"type": "boolean"},
            },
            "required": ["pod"],
        },
        "annotations": READ,
    },
    {
        "name": "kube_events",
        "description": "Recent cluster events, newest last.",
        "inputSchema": {"type": "object", "properties": {"namespace": {"type": "string"}}},
        "annotations": READ,
    },
    {
        # Deliberately not annotated read: this returns credential material.
        "name": "kube_secret_get",
        "description": "Read one Secret as YAML. Contains credentials; ask before using it.",
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "namespace": {"type": "string"}},
            "required": ["name"],
        },
        "annotations": WRITE,
    },
    {
        "name": "kube_apply",
        "description": "Apply a YAML manifest.",
        "inputSchema": {
            "type": "object",
            "properties": {"manifest": {"type": "string"}, "namespace": {"type": "string"}},
            "required": ["manifest"],
        },
        "annotations": WRITE,
    },
    {
        "name": "kube_delete",
        "description": "Delete one named resource. Never a whole namespace.",
        "inputSchema": {
            "type": "object",
            "properties": {"resource": {"type": "string"}, "name": {"type": "string"}, "namespace": {"type": "string"}},
            "required": ["resource", "name"],
        },
        "annotations": WRITE,
    },
    {
        "name": "kube_scale",
        "description": "Scale a workload.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "resource": {"type": "string"},
                "name": {"type": "string"},
                "namespace": {"type": "string"},
                "replicas": {"type": "integer"},
            },
            "required": ["name", "replicas"],
        },
        "annotations": WRITE,
    },
    {
        "name": "kube_rollout",
        "description": "Restart, check the status of, or undo a rollout.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string"},
                "resource": {"type": "string"},
                "name": {"type": "string"},
                "namespace": {"type": "string"},
            },
            "required": ["name"],
        },
        "annotations": WRITE,
    },
]

HANDLERS = {
    "kube_get": tool_get,
    "kube_describe": tool_describe,
    "kube_logs": tool_logs,
    "kube_events": tool_events,
    "kube_secret_get": tool_secret_get,
    "kube_apply": tool_apply,
    "kube_delete": tool_delete,
    "kube_scale": tool_scale,
    "kube_rollout": tool_rollout,
}


def handle(message: dict) -> dict | None:
    method = message.get("method")
    request_id = message.get("id")
    if request_id is None:  # notification
        return None
    if method == "initialize":
        version = (message.get("params") or {}).get("protocolVersion") or DEFAULT_PROTOCOL
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        }
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = message.get("params") or {}
        name = params.get("name")
        handler = HANDLERS.get(str(name))
        if handler is None:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32602, "message": f"unknown tool {name!r}"},
            }
        try:
            ok, text = handler(params.get("arguments") or {})
        except Exception as exc:  # noqa: BLE001 - a tool failure must not kill the server
            ok, text = False, f"{type(exc).__name__}: {exc}"
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"content": [{"type": "text", "text": text}], "isError": not ok},
        }
    if method == "ping":
        return {"jsonrpc": "2.0", "id": request_id, "result": {}}
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32601, "message": f"method not found: {method}"},
    }


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue
        response = handle(message)
        if response is not None:
            sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()

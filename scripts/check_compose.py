"""Static checks over the compose files. Run inside the repo-check container."""
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
BASE = ROOT / "docker-compose.yml"
HOSTED = ROOT / "docker-compose.hosted.yml"
OVERRIDE = ROOT / "scripts" / "hosted-check.override.yml"
TEST_PROJECTS = ("coherence-standalone-test", "coherence-standalone-fresh")

errors = []


def check(ok, msg):
    if not ok:
        errors.append(msg)


class Loader(yaml.SafeLoader):
    """Reads compose-only tags (!reset, !override) as their plain value."""


def _tagged(loader, suffix, node):
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node, deep=True)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    return loader.construct_scalar(node)


Loader.add_multi_constructor("!", _tagged)


def load(path):
    check(path.is_file(), f"missing file: {path.relative_to(ROOT)}")
    if not path.is_file():
        return {}
    with path.open(encoding="utf-8") as fh:
        return yaml.load(fh, Loader=Loader) or {}


def env_of(service):
    env = service.get("environment") or {}
    if isinstance(env, list):
        env = dict(item.split("=", 1) for item in env)
    return env


def merged(files):
    out = {}
    for doc in files:
        for name, svc in (doc.get("services") or {}).items():
            out.setdefault(name, {}).update(svc or {})
    return out


base, hosted, override = load(BASE), load(HOSTED), load(OVERRIDE)
services = merged([base, hosted, override])

# every service is memory-limited and not pinned to cpus
for name, svc in sorted(services.items()):
    check("mem_limit" in svc, f"{name}: no mem_limit")
for doc, path in ((base, BASE), (hosted, HOSTED), (override, OVERRIDE)):
    for name, svc in (doc.get("services") or {}).items():
        for key in svc or {}:
            check(
                not (key in ("cpuset", "cpus") or key.startswith("cpu_")),
                f"{path.name}: {name} sets {key}",
            )

# cloudflared exists only in the wan profile, and only in the hosted file
check("cloudflared" in (hosted.get("services") or {}), "hosted file has no cloudflared")
for doc, path in ((base, BASE), (override, OVERRIDE)):
    check("cloudflared" not in (doc.get("services") or {}), f"{path.name} defines cloudflared")
check(
    services.get("cloudflared", {}).get("profiles") == ["wan"],
    "cloudflared is not in profile wan only",
)
tunnel = services.get("cloudflared", {})
check(tunnel.get("read_only") is True, "cloudflared is not read_only")
check(env_of(tunnel).get("TUNNEL_TOKEN") == "${TUNNEL_TOKEN:-}", "cloudflared token is not fail-closed")

# hosted engine and web cannot write to disk
hosted_svcs = hosted.get("services") or {}
engine = hosted_svcs.get("engine", {})
check(env_of(engine).get("MESSAGE_INTERVAL_SECONDS") == "15", "hosted engine interval is not the string 15")
for key, want in (("COHERENCE_MODE", "hosted"), ("HF_HUB_OFFLINE", "1"), ("PYTHONDONTWRITEBYTECODE", "1")):
    check(env_of(engine).get(key) == want, f"hosted engine {key} is not {want}")
check(not engine.get("volumes"), "hosted engine keeps volumes")
check(
    env_of(engine).get("TRUSTED_PROXY_HOPS") == "2",
    "hosted engine TRUSTED_PROXY_HOPS is not 2",
)
check(
    not any("${" in str(value) for value in env_of(engine).values()),
    "hosted engine environment contains an interpolation",
)
check(
    engine.get("env_file") == [".env.example"],
    "hosted engine env_file is not .env.example only",
)
base_web_env = env_of((base.get("services") or {}).get("web", {}))
for key, want in (
    ("ENGINE_URL", "${ENGINE_URL:-http://engine:8000}"),
    ("BODY_CAP_BYTES", "${BODY_CAP_BYTES:-134217728}"),
):
    check(base_web_env.get(key) == want, f"web {key} is not {want}")
check((base.get("services") or {}).get("engine", {}).get("mem_limit") == "2g", "engine mem_limit is not 2g")
check((base.get("services") or {}).get("engine-e2e", {}).get("mem_limit") == "2g", "engine-e2e mem_limit is not 2g")
e2e_env = env_of((base.get("services") or {}).get("engine-e2e", {}))
for key, want in (("ENGINE_FAKE_CONTEXT_TOKENS", "300"), ("ENGINE_FAKE_STRONG_TIER", "1"),
                  ("BODY_CAP_BYTES", "134217728")):
    check(e2e_env.get(key) == want, f"engine-e2e {key} is not {want}")
for name in ("engine", "web"):
    svc = hosted_svcs.get(name, {})
    check(svc.get("read_only") is True, f"hosted {name} is not read_only")
    check(svc.get("tmpfs") == ["/tmp:size=64m"], f"hosted {name} tmpfs is not /tmp:size=64m")
    check(not svc.get("volumes"), f"hosted {name} has volumes")
    check(svc.get("restart") == "unless-stopped", f"hosted {name} restart is not unless-stopped")
    check(env_of(svc).get("BODY_CAP_BYTES") == "16777216", f"hosted {name} BODY_CAP_BYTES is not the string 16777216")

# only the tunnel container may name the client, and it shares the web's network
check(
    env_of(hosted_svcs.get("web", {})).get("TUNNEL_HOST") == "cloudflared",
    "hosted web TUNNEL_HOST is not cloudflared",
)
check("TUNNEL_HOST" not in base_web_env, "base web sets TUNNEL_HOST")
check(
    "TUNNEL_HOST" not in env_of((override.get("services") or {}).get("web", {})),
    "the override sets web TUNNEL_HOST",
)
check("networks" not in tunnel and "network_mode" not in tunnel, "cloudflared leaves the default network")

# the hosted check adds its own runner and leaves e2e alone
over = override.get("services") or {}
check("e2e" not in over, "the override re-declares e2e")
runner = over.get("e2e-hosted", {})
check(bool(runner), "the override has no e2e-hosted")
check(env_of(runner).get("BASE_URL") == "http://web:8080", "e2e-hosted BASE_URL is not http://web:8080")
check(runner.get("mem_limit") == "2g", "e2e-hosted mem_limit is not 2g")
check(list((runner.get("depends_on") or {})) == ["web"], "e2e-hosted does not depend on web only")
check("./examples:/examples:ro" in (runner.get("volumes") or []), "e2e-hosted does not mount ./examples read-only")
check(
    runner.get("build") == (base.get("services") or {}).get("e2e", {}).get("build"),
    "e2e-hosted build differs from e2e",
)
check(env_of(over.get("engine", {})).get("ENGINE_FAKE_PROVIDER") == "1", "the override does not enable the fake provider")

# a user's keys volume must survive every test run
for doc, path in ((base, BASE), (hosted, HOSTED), (override, OVERRIDE)):
    for vol, spec in (doc.get("volumes") or {}).items():
        check(not (spec and "name" in spec), f"{path.name}: volume {vol} has an explicit name")

for script in sorted((ROOT / "scripts").iterdir()):
    if script.name == Path(__file__).name or not script.is_file():
        continue
    text = script.read_text(encoding="utf-8").replace(chr(92) + chr(10), " ")
    for line in text.splitlines():
        match = re.search(r"\bdown\b.*\s-v\b", line)
        if not match:
            continue
        before = line[: match.start()]
        ok = any(re.search(rf"-p\s+{re.escape(p)}\b", before) for p in TEST_PROJECTS)
        check(ok, f"{script.name}: down -v without a test project name: {line.strip()}")

if errors:
    print("\n".join(errors))
    sys.exit(1)
print(f"compose checks passed ({len(services)} services)")

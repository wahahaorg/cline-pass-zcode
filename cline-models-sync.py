#!/usr/bin/env python3
"""Sync the ZCode Cline provider model list with the Cline Pass catalog.

The gateway's `/v1/models` lists the whole OpenRouter-style catalog (~470
models). Most of those need Cline Credits or are only callable from Cline's own
product surfaces. The subscription models are published separately:

    GET https://api.cline.bot/api/v1/ai/cline/recommended-models

Only the `clinePass` array is used here. Each candidate is probed with one
minimal chat completion before it is written, so region-blocked or retired
models never reach the config.

Usage:
    python3 cline-models-sync.py                  # sync + verify
    python3 cline-models-sync.py --dry-run        # show what would change
    python3 cline-models-sync.py --no-verify      # trust the catalog as-is
    python3 cline-models-sync.py --keep-missing   # keep models no longer listed
"""

from __future__ import annotations

import argparse
import collections
import copy
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

CONFIG_PATH = Path.home() / ".zcode" / "v2" / "provider_config.json"
CATALOG_URL = "https://api.cline.bot/api/v1/ai/cline/recommended-models"
CHAT_URL = "https://api.cline.bot/api/v1/chat/completions"
PROVIDER_NAME = "cline"
MODEL_PREFIX = "cline-pass/"

# Published context windows. Models missing here are still synced; they simply
# carry no contextWindow property, so ZCode falls back to its own default.
CONTEXT_WINDOW = {
    "cline-pass/deepseek-v4.1-flash": 1_000_000,
    "cline-pass/deepseek-v4-pro": 1_000_000,
    "cline-pass/glm-5.3": 1_000_000,
    "cline-pass/glm-5.3-flash": 1_000_000,
    "cline-pass/kimi-k3": 1_048_576,
    "cline-pass/qwen3.8-max": 1_000_000,
    "cline-pass/qwen3.7-max": 1_000_000,
    "cline-pass/qwen3.7-plus": 1_000_000,
    "cline-pass/minimax-m3": 1_048_576,
    "cline-pass/mimo-v2.5-pro": 1_048_576,
    "cline-pass/mimo-v2.5": 1_048_576,
}


def load_config(path: Path):
    with path.open() as fh:
        return json.load(fh, object_pairs_hook=collections.OrderedDict)


def find_provider(config):
    for provider in config["config"]["providerConfigRules"]["providerRules"]:
        if provider.get("providerName") == PROVIDER_NAME:
            return provider
    raise SystemExit(f"找不到 providerName={PROVIDER_NAME} 的配置")


def fetch_catalog():
    req = urllib.request.Request(CATALOG_URL, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.load(resp)
    models = payload.get("clinePass") or []
    if not models:
        raise SystemExit("recommended-models 没有返回 clinePass 列表")
    return [m["id"] for m in models if m.get("id", "").startswith(MODEL_PREFIX)]


def classify_error(status: int, body: str):
    """Return (verdict, detail). Verdicts: ok / gone / blocked / unknown."""
    try:
        err = json.loads(body).get("error")
    except Exception:
        err = None
    detail = str(err or body)[:160]
    if status == 404:
        return "gone", detail
    if status in (401, 402, 403):
        return "blocked", detail
    return "unknown", f"HTTP {status}: {detail}"


def probe(api_key: str, model: str, timeout: int = 90):
    body = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": "Reply with the single word: OK"}],
            "max_tokens": 128,
            "stream": False,
        }
    ).encode()
    req = urllib.request.Request(
        CHAT_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            raw = resp.read().decode()
    except urllib.error.HTTPError as exc:
        return classify_error(exc.code, exc.read().decode(errors="replace"))
    except Exception as exc:  # network hiccup, timeout, TLS reset
        return "unknown", f"{type(exc).__name__}: {exc}"[:160]

    try:
        payload = json.loads(raw)
    except Exception:
        return "unknown", "响应不是合法 JSON"

    if payload.get("success") is False or payload.get("error"):
        return "unknown", str(payload.get("error"))[:160]

    data = payload.get("data", payload)
    if status == 200 and (data.get("choices") or []):
        return "ok", ""
    return "unknown", "HTTP 200 但没有 choices"


def zcode_running() -> bool:
    try:
        result = subprocess.run(
            ["pgrep", "-f", "ZCode.app/Contents/MacOS"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return bool(result.stdout.strip())
    except Exception:
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--dry-run", action="store_true", help="只打印，不写配置")
    parser.add_argument("--no-verify", action="store_true", help="不逐个探测模型")
    parser.add_argument("--keep-missing", action="store_true", help="保留已下线但仍在配置里的模型")
    parser.add_argument("--timeout", type=int, default=90, help="单模型探测超时秒数")
    args = parser.parse_args()

    config = load_config(args.config)
    provider = find_provider(config)
    provider_id = provider["providerId"]
    api_key = provider["config"]["access"].get("apiKey", "")
    if not api_key:
        raise SystemExit("cline provider 没有配置 apiKey")

    catalog_ids = fetch_catalog()
    old_ids = list(
        provider["config"].get("personalModelIds")
        or provider["config"].get("modelOrder")
        or []
    )
    old_pass_ids = [m for m in old_ids if m.startswith(MODEL_PREFIX)]
    other_ids = [m for m in old_ids if not m.startswith(MODEL_PREFIX)]

    print(f"官方 clinePass 模型: {len(catalog_ids)} 个")
    for model in catalog_ids:
        print(f"  - {model}")

    verdicts = {}
    if args.no_verify:
        for model in catalog_ids:
            verdicts[model] = ("ok", "")
    else:
        print("\n逐个探测中（每个模型一次最小请求）...")
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = pool.map(lambda m: (m, probe(api_key, m, args.timeout)), catalog_ids)
            for model, result in results:
                verdicts[model] = result
                mark = {"ok": "可用", "gone": "已下线", "blocked": "无权限/区域限制", "unknown": "未确认"}[result[0]]
                extra = f"  {result[1]}" if result[1] else ""
                print(f"  {mark:6} {model}{extra}")

        if not any(v[0] == "ok" for v in verdicts.values()):
            raise SystemExit("\n没有任何模型探测通过，终止且不修改配置。")

    existing_set = set(old_pass_ids)
    keep = set()
    for model in catalog_ids:
        verdict = verdicts[model][0]
        if verdict == "ok":
            keep.add(model)
        elif verdict == "unknown" and model in existing_set:
            # Transient failure: never drop a model that is already configured.
            keep.add(model)
    if args.keep_missing:
        keep.update(old_pass_ids)

    final_ids = [m for m in old_pass_ids if m in keep]
    final_ids += [m for m in catalog_ids if m in keep and m not in existing_set]
    final_ids += other_ids  # preserved custom, non-cline-pass ids

    removed = [m for m in old_pass_ids if m not in keep]
    added = [m for m in final_ids if m not in old_pass_ids and m.startswith(MODEL_PREFIX)]

    rules_key = config["config"]["modelConfigRules"]["providerModelRules"]
    old_rules = {r["modelId"]: r for r in rules_key if r.get("providerId") == provider_id}
    new_rules = []
    for model in final_ids:
        rule = copy.deepcopy(old_rules.get(model, {}).get("config", {}))
        rule.setdefault("enabled", True)
        if model in CONTEXT_WINDOW:
            props = rule.setdefault("properties", {})
            props.setdefault("contextWindow", CONTEXT_WINDOW[model])
        new_rules.append(
            collections.OrderedDict(
                [("modelId", model), ("config", rule), ("providerId", provider_id)]
            )
        )

    new_personal = final_ids
    new_order = final_ids
    new_other_rules = [r for r in rules_key if r.get("providerId") != provider_id]
    new_rules_all = new_other_rules + new_rules

    changed = (
        old_ids != new_personal
        or provider["config"].get("modelOrder") != new_order
        or rules_key != new_rules_all
    )

    print(f"\n配置结果: {len(final_ids)} 个模型")
    print(f"  新增: {', '.join(added) if added else '无'}")
    print(f"  移除: {', '.join(removed) if removed else '无'}")

    if not changed:
        print("\n配置已是最新，无需写入。")
        return
    if args.dry_run:
        print("\n--dry-run：未写入配置。")
        return

    provider["config"]["personalModelIds"] = new_personal
    provider["config"]["modelOrder"] = new_order
    config["config"]["modelConfigRules"]["providerModelRules"] = new_rules_all

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = args.config.with_name(f"{args.config.name}.bak-{stamp}")
    shutil.copy2(args.config, backup)

    tmp = args.config.with_name(args.config.name + ".tmp")
    tmp.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n")
    os.replace(tmp, args.config)

    print(f"\n已写入 {args.config}")
    print(f"备份: {backup}")
    if zcode_running():
        print("提示：ZCode 正在运行，重启 App 后模型列表才会刷新。")


if __name__ == "__main__":
    main()

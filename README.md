# Cline Pass → ZCode 模型同步

把 Cline Pass 订阅里**真正可用**的模型同步到 ZCode 的 Cline provider 配置。

## 背景

Cline 网关有两个容易混淆的模型接口：

| 接口 | 内容 |
| --- | --- |
| `GET /api/v1/models` | 网关全量目录，几百个模型。大部分需要额外 credits，或者只能在 Cline 自家客户端里用。 |
| `GET /api/v1/ai/cline/recommended-models` | 按套餐分组。`clinePass` 数组才是订阅包含的模型。 |

ZCode 里如果直接点“拉取模型”，走的是第一个接口，结果就是列表很长、但绝大多数调用返回 `402 Insufficient balance` 或 `403 only available via Cline product surfaces`。

这个脚本只读第二个接口，并且在写入配置前**逐个发一次最小请求实测**，所以区域限制、已下线的模型不会被写进去。

## 快速开始

```bash
# 预览，不写配置
python3 cline-models-sync.py --dry-run

# 同步并写入
python3 cline-models-sync.py

# 重启 ZCode，让模型列表刷新
```

脚本默认读取 `~/.zcode/v2/provider_config.json`，从 `providerName: cline` 的 provider 里取 `apiKey`，不需要额外设置环境变量。配置在别的位置时用 `--config` 指定。

## 选项

| 参数 | 作用 |
| --- | --- |
| `--dry-run` | 只打印探测结果和变更，不写文件 |
| `--no-verify` | 不逐个探测，直接按官方 `clinePass` 清单同步 |
| `--keep-missing` | 保留已从官方清单消失、但仍在本地配置里的模型 |
| `--config PATH` | 指定 ZCode 配置文件 |
| `--timeout N` | 单模型探测超时，默认 90 秒 |

## 它做了什么

1. 拉取 `recommended-models`，只取 `clinePass`。
2. 对每个模型发一次 `chat/completions`（`max_tokens=128`）做实测。
3. `403` / `404` / `402` 判定为不可用并剔除；网络抖动等未确认状态会保留已在配置里的模型，避免误删。
4. 保留现有模型顺序，新模型追加到末尾。
5. 已知模型自动补 `contextWindow`，未知的不乱填。
6. 只有内容变化时才写文件；写前生成带时间戳的备份，再用原子替换落盘。
7. 如果所有模型都探测失败（Key 失效、网关故障等），直接终止，不改配置。

## 当前模型

见 [MODELS.md](MODELS.md)。最近一次快照（2026-10-17）：官方清单 14 个，实测可用 13 个；`muse-spark-1.3-contributor` 因区域限制返回 403。

## 注意

- 改完配置需要**重启 ZCode** 才会刷新模型列表。
- 不要在 ZCode 里点“拉取模型”，它会重新拉 `/v1/models` 全量目录。
- 模型可用性受套餐、所在区域和 Cline 侧变更影响，脚本的实测结果只代表运行当时。
- 脚本只修改本地 ZCode 配置，不代理请求，不上传 API Key。Key 只在探测请求的 `Authorization` 头里使用，不会打印或写入输出。
- 非官方工具，与 Cline、ZCode 均无隶属关系。

## 定时同步

每周一上午 10 点跑一次（把路径改成你本地的位置）：

```cron
0 10 * * 1 /opt/homebrew/bin/python3 /path/to/cline-models-sync.py >> /tmp/cline-models-sync.log 2>&1
```

## English

A small utility that syncs the models actually included in a Cline Pass subscription into ZCode's provider config. It reads the `clinePass` array from Cline's `recommended-models` endpoint, probes each model with one minimal request, and writes only the usable ones. `/api/v1/models` is deliberately ignored: it lists the whole gateway, where most entries require extra credits or are restricted to Cline's own product surfaces.

```bash
python3 cline-models-sync.py --dry-run
python3 cline-models-sync.py
```

Requires Python 3.9+ and an existing `cline` provider in ZCode's `provider_config.json`. Restart ZCode after syncing.

## License

MIT

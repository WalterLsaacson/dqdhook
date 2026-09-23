# 部署到新服务器

目标：在另一台 Linux 机器上 `git clone` 后，按本文跑出与当前生产机相同的一套服务（Hub + 看板 + 进程内 match-bridge + mqtt 门控 + live 询价下单）。

不要同时在两台机器上用**同一把钱包**开 live。先停旧机，再启新机。

---

## 1. 机器要求

| 项 | 建议 |
|---|---|
| 系统 | Linux + systemd（Ubuntu 22.04 / Debian 12 一类即可） |
| 权限 | 能写仓库目录；装常驻服务需要 `root`（写 `/etc/systemd/system`） |
| 内存 | mqtt 门控约 1–2G 即可；若改 `QUOTE_GATE_SOURCE=dom` 开 Chromium，按 4G+ 预留 |
| Python | 不必预装 3.11；`./scripts/dqdhook.sh deps` 用 `uv` 装 CPython 3.11 到 `.venv` |
| 出网 | 必须直连下列域名（本机生产 **不走代理**，runner 会强制 `PM_PROXY=none`） |
| 入网 | 只看看板才需要开 `8787–8792/tcp`；交易本身不依赖入站 |

出网清单：

| 用途 | 主机 |
|---|---|
| CLOB 下单 | `clob.polymarket.com` |
| Gamma 赛程快照 | `gamma-api.polymarket.com` |
| 懂球帝比分 | 懂球帝 `match_list` 公网 API |
| 纳米门控 MQTT | `wss://trackermq.namitiyu.com/mqtt` |
| API-Football | `https://v3.football.api-sports.io` |
| 安装依赖 | `github.com`、`astral.sh`、PyPI |

时区可以保持 UTC。配对用的北京时间在代码里写死 `UTC+8`，不依赖系统 TZ。

---

## 2. Clone

仓库私有，新机器需要 GitHub 读权限（deploy key 或 HTTPS token）。

```bash
# 与生产相同的功能分支；提交后请改成当时 origin 上的最新 commit
git clone git@github.com:WalterLsaacson/dqdhook.git
cd dqdhook
git checkout feat/host-and-bridge-warm-20260906
```

HTTPS 也可以：`https://github.com/WalterLsaacson/dqdhook.git`。

只启动一个 `run_main`。不要再单独开 `pm_quote`、skill host 或第二个 `frontend/run_main.py`（`:8790` 占用会直接退出）。

---

## 3. 配置 `.env`

```bash
cp .env.example .env
chmod 600 .env
```

必填（没有则 runner 会加 `--no-trade`，只看板不下单）：

| 变量 | 说明 |
|---|---|
| `PRIVATE_KEY` | 导出钱包私钥，`0x` + 64 位 hex |
| `FUNDER` | Polymarket 个人资料页钱包地址（`SIGNATURE_TYPE=3` 必填） |
| `apifootball_key` | [api-sports.io](https://dashboard.api-football.com/) API Key；门控认分 / T+10 比分校验都靠它 |

`.env.example` 里其余金额、地板价、T+10 延迟已按当前生产填好，保持即可：

| 旋钮 | 生产值 | 含义 |
|---|---|---|
| `QUOTE_GATE_SOURCE` | `mqtt` | 纳米 MQTT 门控，**不启动 Chromium** |
| `QUOTE_DQD_STREAM_OBSERVE` | `1` | 进球门控总开关 |
| `QUOTE_REST_ENABLED` | `0` | 进球通道不挂 0.99 rest（T+10 仍会独立挂） |
| `QUOTE_T10_USDC` | `100` | T+10 FAK 与 0.99 GTC **各**用该金额 |
| `QUOTE_T10_DELAY_S` | `480` | 进球后再扫，8 分钟 |
| `QUOTE_GATE_MIN_BUY_PRICE` | `0.30` | 门控 / T+10 低于 0.30 不买不挂 |
| `QUOTE_MIN_BUY_PRICE` | `0.6` | 仅 leftover 路径 |
| `QUOTE_GOAL_MAX_USDC` | `2` | 进球 FAK |
| `QUOTE_FT_MAX_USDC` | `100` | 终场 FAK |
| `QUOTE_LOCKED_SWEEP_USDC` | `100` | 已锁定 WIN 扫盘 |
| `QUOTE_FT_DUST_USDC` | `0` | 终场灰尘盘关闭 |
| `PM_PROXY` | `none` | `dqdhook-run.sh` 也会再强制一次 |

不要填已过期的 `ODDS_API_IO_KEY`（没 key 时自动跳过，不发请求）。

私钥、`FUNDER`、API Key 只放 `.env`，Git 已忽略。不要写进文档或提交。

---

## 4. 安装依赖并常驻

```bash
chmod +x scripts/dqdhook.sh scripts/dqdhook-run.sh

./scripts/dqdhook.sh deps      # uv + CPython 3.11 venv + pip + Playwright Chromium
sudo ./scripts/dqdhook.sh install   # 写 systemd unit dqdhook.service 并开机自启
./scripts/dqdhook.sh start
./scripts/dqdhook.sh status
./scripts/dqdhook.sh urls
```

`install` 必须用**仓库所在路径**执行（脚本会把绝对路径写进 unit）。不要从旧机器拷 `/etc/systemd/system/dqdhook.service`：里面的 `WorkingDirectory`、`DQD_PYTHON`、`DQD_PUBLIC_HOST` 都是旧机的。

没有 systemd 时，`start` 会退回 `nohup`（日志 `data/run/dqdhook.nohup.log`）。

日常运维：

```bash
./scripts/dqdhook.sh restart
./scripts/dqdhook.sh stop
./scripts/dqdhook.sh logs     # journalctl -u dqdhook -f
./scripts/dqdhook.sh urls
```

改 `.env` 或代码后必须 `restart` 才生效。

---

## 5. 核对与当前生产一致

浏览器打开 Hub（`./scripts/dqdhook.sh urls` 打印的地址，默认 `:8790`）。

| 检查 | 期望 |
|---|---|
| Hub | Quote 进程 up；Trade `goals:live ft:live`；Boards 5/5 |
| 监听 | `ss -lntp` 可见 `8787 8788 8789 8790 8791 8792` |
| 启动日志 | `data/run/hub.log` 有 `dqdhook-run: bind=0.0.0.0` |
| 询价日志 | `data/pm-quote/watch.log` 出现 `gate_min_buy_price=0.3`、`QUOTE_GATE_SOURCE=mqtt` |
| Bridge | `:8789` 有配对场（Gamma 快照最多约 3 小时才会写满 `data/polymarket/snapshot.json`） |
| Pitch Gate | 进球后 `:8791` 出帧；mqtt 挂了是 `unavailable`，**不会**回退 DOM |
| 成交 | live 成功写入 `data/pm-quote/trades.jsonl`，字段 `live: true` |

可选冒烟（不下单）：

```bash
.venv/bin/python .cursor/skills/polymarket-quote/scripts/smoke_trade_modes.py
.venv/bin/python .cursor/skills/polymarket-quote/scripts/smoke_half_settle.py
.venv/bin/python .cursor/skills/polymarket-quote/scripts/smoke_t10_scan.py
.venv/bin/python .cursor/skills/polymarket-quote/scripts/smoke_nami_mqtt.py
```

先观察、后 live：把 `.env` 里私钥拿掉，或

```bash
DQD_EXTRA_ARGS='--no-trade' ./scripts/dqdhook.sh restart
```

确认配对和门控正常后再去掉 `DQD_EXTRA_ARGS`。

---

## 6. 从旧机迁状态（要「接着跑」时）

全新 clone 也能交易：bridge / Gamma / AF 缓存会自己暖起来。若迁移时场次正在进行，需要把仓位和未到期 T+10 带过去，否则会漏扫、重复买、或冻不住半场比分。

**同一钱包：先停旧机，再拷文件，再启新机。**

在旧机：

```bash
./scripts/dqdhook.sh stop
```

拷到新机仓库（示例：旧机 `old`，新机已 clone 到 `/root/workspace/dqdhook`）：

```bash
NEW=root@NEW_HOST:/root/workspace/dqdhook

# 密钥：用 scp，不要贴聊天记录
scp .env "$NEW/.env"

# 交易连续性（建议）
scp data/pm-quote/open_positions.json "$NEW/data/pm-quote/"
scp data/pm-quote/t10_pending.json "$NEW/data/pm-quote/"
scp data/pm-quote/cursor.json "$NEW/data/pm-quote/"
scp data/pm-quote/af_confirmed_scores.json "$NEW/data/pm-quote/"
scp data/bridge/half_scores.json data/bridge/prev_scores.json \
    data/bridge/prev_period.json data/bridge/prev_status.json \
    "$NEW/data/bridge/"

# 少打 AF 额度（建议）
scp data/apifootball/fixture_cache.json "$NEW/data/apifootball/"
rsync -av data/apifootball/date_fixtures/ "$NEW/data/apifootball/date_fixtures/"

# 分析 continuity（可选）
scp data/pm-quote/trades.jsonl "$NEW/data/pm-quote/"
```

不要拷：

- `data/run/`、`watch.log`、各类 `*.jsonl` 观察日志
- 旧机的 `dqdhook.service`（必须在新机 `install`）
- `data/polymarket/snapshot.json`（可拷以缩短第一次配对等待；不拷则 polymarket-board 最多约 3h 刷新）

新机 `chmod 600 .env` 后 `./scripts/dqdhook.sh start`。确认 Hub 正常后，旧机保持 `stop`，并 `./scripts/dqdhook.sh uninstall`（避免开机又拉起第二套 live）。

---

## 7. 端口与防火墙

| 端口 | 服务 |
|---|---|
| 8790 | Hub / 成交账本 `/trades` |
| 8787 | 懂球帝看板 |
| 8788 | Polymarket 看板 |
| 8789 | Match Bridge |
| 8791 | Pitch Gate |
| 8792 | API-Football Bridge |

云厂商安全组按需放行 `8787-8792/tcp`。只本机看可用 SSH 隧道：

```bash
ssh -N -L 8790:127.0.0.1:8790 NEW_HOST
# 浏览器打开 http://127.0.0.1:8790/
```

看板绑 `0.0.0.0`。Hub 上的公网链接来自 `DQD_PUBLIC_HOST`（`install` 时写入 unit）。换 IP 后重新 `install`，或在 `.env` 里设 `DQD_PUBLIC_HOST`。

---

## 8. 故障对照

| 现象 | 处理 |
|---|---|
| 启动即 `--no-trade` | 仓库根没有 `.env`，或缺 `PRIVATE_KEY` / `FUNDER` |
| `:8790` 立刻退出 | 已有一个 `run_main`；`./scripts/dqdhook.sh status` 后先 `stop` |
| 门控 `unavailable` | mqtt 出网被拦，或 `QUOTE_GATE_SOURCE` 不是 `mqtt`。不会自动改 DOM |
| Bridge 长时间 0 配对 | 等 `data/polymarket/snapshot.json`；或从旧机拷一份 |
| AF 不认分 / T+10 不买 | `.env` 的 `apifootball_key`；免费档约 100 次/天 |
| CLOB 签名失败 | `SIGNATURE_TYPE` 与钱包类型不一致；`FUNDER` 不是资料页地址 |
| 看板 IP 不对 | 重新 `./scripts/dqdhook.sh install`，或设 `DQD_PUBLIC_HOST` |
| 想改回 Chromium 门控 | `.env` 设 `QUOTE_GATE_SOURCE=dom` 后 restart；需要 `deps` 装好的 Chromium 和更大内存 |

策略与环境变量细节见仓库根 [README.md](README.md)。

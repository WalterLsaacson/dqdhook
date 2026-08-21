# dqdhook

懂球帝 ↔ Polymarket 足球流水线：实时比分、赛事配对、动画状态门控后的 CLOB 询价与下单。

---

## 架构总览

```text
懂球帝 match_list          Polymarket Gamma（3h 快照）
        │                           │
        └──────── match-bridge ─────┘
                     │
                     │ 内存 event_queue（默认）
                     ▼
              pm_quote watch
                     │
         ┌───────────┼───────────┐
         ▼           ▼           ▼
   进球 pitch-gate   终场立刻询价   回撤取消门控/rest
   （读动画 DOM）     （无门控直接询价）（不自动平仓）
```

| 模块 | 作用 |
|---|---|
| `dongqiudi-match` | 懂球帝赛程 / 比分 / 比分变化 |
| `polymarket-soccer` | Polymarket Gamma 足球对阵快照 |
| `match-bridge` | DQD ↔ PM 模糊配对；发出 `score_change` / `match_finished` |
| `pitch-state` | 动画状态判定规则表：是否恢复比赛（`in_play` / `stopped` / `unclear`） |
| `polymarket-quote` | 门控询价 + 进程内下单（默认 goals/ft 均为 live） |
| `trade-analytics` | 历史成交 / 估算盈亏分析（独立） |

**唯一启动入口**：`python3 frontend/run_main.py`（System Main）。

它会拉起：

| 服务 | 端口 | 说明 |
|---|---|---|
| Hub | `:8790` | 总控页 |
| 懂球帝看板 | `:8787` | DQD 赛程 |
| Polymarket 看板 | `:8788` | Gamma 快照 |
| Match Bridge 看板 | `:8789` | 配对结果（只读；quote 持有桥） |
| Pitch Gate 看板 | `:8791` | 每球逐帧状态 + 判定（可筛 in_play / 回撤） |
| `pm_quote watch` | — | **进程内持有 match-bridge**，内存队列直达询价 |

不要单独再开 `pm_quote`、boards skill host，或第二个 `run_main`（`:8790` 占用会直接退出）。  
`MAIN_BRIDGE_INPROC=0` 可回退为 bridge-board 文件唤醒（延迟更大，不推荐）。

---

## 整套监控下单策略

### 1. 数据与触发

1. **懂球帝**以约 5s（有进行中）/ 60s（空闲）节奏刷新 `full` 页签；加时阶段也会保持较快轮询。
2. **Polymarket** 对阵由 polymarket-board 约每 **3 小时**写 `data/polymarket/snapshot.json`；bridge 只读快照做配对，不扫 Gamma 联赛列表。
3. **配对**：英队名 + 北京开球时间模糊匹配（`min_score` / `min_side` / 联赛别名等，见 `match-bridge`）。未配对场次**不会**进门控下单。
4. Bridge 产出事件：
   - `score_change` + 进球（比分上升）→ 启动 **pitch-gate**
   - `score_change` + 回撤（任一侧比分下降）→ **取消**该场门控与 rest；**不自动 flatten**
   - `match_finished`（`period` 切入 `FT`）→ **立刻询价下单**（不过门控）
5. 加时中（DQD 仍在踢、`minute>90` 且 `injury_time==0`）的比分抖动**不发事件**，避免误触发。

### 2. 进球通道（pitch-gate，核心）

对每一个已配对的 DQD 进球：

| 步骤 | 行为 |
|---|---|
| 启动 | `PitchGateCoordinator.start_gate`；**此时不下单** |
| 采样 | 进球后 **+5s** 起第一次读数，之后每 **5s** 一次，直到 **150s** 超时或回撤取消 |
| 判定 | 直接读动画 DOM 文本：关键词（进攻/控球等）+ **底部比分必须等于 DQD 期望比分**（精确值，非 OCR） |
| 确认 | **首帧合格即可**（`GATE_CONFIRM_FRAMES=1`），不再要求连续两帧 |
| 下单 | `_quote_one`（`trade_context.pitch_gate=true`），**每球最多一刀**；买后仍继续采样直到超时（供看板复盘） |
| VAR | 采样过程中任一帧判为 **VAR** → 该球 **永久不下单**（`pitch_gate_var_veto`），仍采满超时窗口 |
| 超时 | 全程没有合格 `in_play` → `pitch_gate_timeout`，不买 |
| 回撤 | DQD 回撤 → 取消会话 `pitch_gate_canceled`；**买后 300s 内**则立即平仓（见下节） |

**判定源：读动画 DOM，不再截图、不再跑 OCR**

动画自己就把状态渲染成文字和 CSS class，OCR 过去只是在从像素里把这段文字还原回来。现在直接读：

| 读什么 | 例子 | 用途 |
|---|---|---|
| `.pop-box` 文本 | `皮尔利斯 危险进攻` / `VAR 回看` | 进行中 / 停止 关键词，与 OCR 用同一张关键词表 |
| `.center-box` 文本 | `78:57 1 : 0` | 比赛时钟 + 底部比分（**精确值**，不存在误读） |
| 状态 class | `possession-rect` / `dangerous-attack-move` | 看板展示；文案闪事件时的补充信息 |

- 整场门控**只开一个页面**：开页 ~1.7–2.4s（与 +5s 首帧延迟重叠），之后每次读数 **2–8ms**；旧路径是每帧 ~4.4s 加载 + ~3.8s OCR。实测端到端在 **t+5.01s** 完成买入。
- 页面来源仍是 `match_list` 每行自带的 `animation_live` 直链（`tracker.namitiyu.com/...&id=<nami_id>`），**现场直播的场次一样有动画**。读不到时会回退去找懂球帝页面里的 `iframe.md-anim-iframe`。
- 覆盖率实测：全部场次 91%，已配对 Polymarket 的场次 **76/77**，缺失集中在友谊赛和业余级别。
- 15s（`QUOTE_DOM_OPEN_TIMEOUT_S`）内找不到动画 → `unavailable`，不下单。
- 换回旧路径：`QUOTE_GATE_SOURCE=ocr`（截图 + PaddleOCR 代码完整保留，只是默认不走）。

**为什么换**：在 3 场直播共 24 帧上同时跑两种判定，一致率 88%；3 次分歧**全部**是 OCR 没读出比分而 DOM 读得准确，反向一次没有。由于门控要求「in_play 且比分相符」，这些漏判不会误开门，但会把入场推迟到下一个 5s 采样点。

**门控通过条件（须同时满足）：**

- DOM 状态文本命中「进行中」类关键词（进攻 / 控球 / 任意球等），且非 VAR/换人/庆祝等停止态  
- 底部比分 = 该球期望的 `home_score-away_score`（比分未更新或不一致 → 不当作 `in_play`）  
- 页面时钟相对上一次读数**有推进**（防止卡死页面一直重复最后状态）  
- 本会话**从未**出现过 VAR  

**明确不下单的情况：**

| 情况 | 结果 |
|---|---|
| 未配对 Polymarket | 不进门控 |
| 缺 `QUOTE_DQD_STREAM_OBSERVE` | `pitch_gate_unavailable` |
| 打不开动画页（15s 内没有 `.football-animate`） | `dom_reader: no_animation_frame`，不买 |
| 页面时钟不推进（卡死） | `stale_page`，继续采样但不买 |
| 事件过旧（默认 >900s） | `goal_stale` 跳过 |
| 比分对不上 | 继续采样，不买 |
| 抓帧中出现 VAR | `var_veto`，该球不买 |
| 懂球帝回撤该球（买入前） | 取消门控 + 撤销未 drain 的买信号 |
| 懂球帝回撤该球（买入后 300s 内） | **立即 FAK 平仓**（见下节） |
| 动画已改比分、随后 DQD 才长延迟回撤 | 买入时无法预知；由买后保护窗口兜底 |
| VAR 出现在**已经下单之后** | 拦不住该刀（除非随后 DQD 回撤触发保护窗口） |

### 3. 终场通道（FT）

- `match_finished` 到达后**立即**按终场比分解读盘口、询价；**无** pitch-gate。
- 默认 **live**；同 `match_id` 终场只处理一次（`cursor.processed_ft_match_ids`）。
- 过旧事件同样受 `QUOTE_FT_MAX_AGE_S`（默认 900s）约束。

### 4. 回撤、买后保护与持仓

**买后保护窗口**是延迟回撤的主要防线：买入那一刻无法知道十几秒后懂球帝会不会推翻进球，所以风险在出场端处理，而不是靠拖延买点。

| 动作 | 行为 |
|---|---|
| DQD 回撤 | 取消该场 **rest 限价** + **进行中的 pitch-gate** + 撤销未 drain 的买信号 |
| 回撤发生在门控买入后 **`QUOTE_GATE_PROTECT_S`（默认 300s）** 内 | **立即 FAK 平仓**（`gate_protect_reversal`），dry lot 只记 `flatten_dry_run` |
| 回撤超出该窗口，或是 FT（非门控）持仓 | 不动，沿用终场 `ft_reversal_vs_entry`（届时已近归零，只是清仓释放额度） |
| `QUOTE_GATE_PROTECT_S=0` | 关闭买后保护，退回「一律等终场」 |
| 同场新进球 | 取消该场先前未完成的门控会话（`superseded_by_new_goal`） |

平仓卖出沿用既有风控：按 `entry×80%` 设最低卖价，不做 0.01 甩卖；吃不掉的残量留给后续 tick 重试。持仓记录在 `data/pm-quote/open_positions.json`，带 `pitch_gate` 与 `opened_at` 两个字段用于判定窗口。

Pitch Gate 看板：普通回撤为**橙色**；若该球曾判定过 `in_play` 后又回撤，列表按钮/徽章为**红色**（「回撤·曾in_play」）。

### 5. 询价与成交规则

触发询价后（门控确认的进球，或终场）：

1. 用 bridge 的 `event_id` / `market_refs` + 预热缓存 `data/pm-quote/market_cache/{match_id}.json` 拉盘口定义。  
2. 按当前比分结算各 token：`WIN` / `LOSE` / `PENDING`。  
3. **只交易 `buy_win`**（买已锁定为 WIN 的一侧）；`sell_lose` 已关闭。  
4. CLOB：`POST /books` 批量吃盘；默认 **`walk`** 深度（受 `max_levels` / `max_usdc` / `max_shares` / `max_slippage` 约束），FAK 市价。  
5. 手续费模型：`fee ≈ feeRate × p × (1−p)`（默认 `feeRate=0.05`）；需 `net_edge ≥ min_net`（默认约 0.0076）才算 misprice。  
6. **Pitch-gate 确认单**跳过 `min_buy_price`（默认 0.6）以及部分 $1 尺寸地板；仍受 `QUOTE_MAX_USDC` / fee / `min_net` 约束。  
7. 极端价（≤0.01 或 >0.992）默认跳过，除非 `--allow-extreme-prices`。  

**涵盖盘口（有则报）：** 胜平负六 token、大小球（含球队/半场）、BTTS、准确比分等（见 `polymarket-quote/reference.md`）。

**限价 rest（可选，默认关）：**

- 需 `QUOTE_REST_ENABLED=1`  
- 门控 WIN 无法 FAK 成交时，挂 **@0.99**、**≥5 shares**（约 $4.95）、`GTD`，过期默认 3600s  
- DQD 回撤会取消这些 rest  

### 6. 模式与仓位

| 通道 | 默认 | 说明 |
|---|---|---|
| 进球 goals | **live** | 仍须过 pitch-gate 才买 |
| 终场 ft | **live** | 不过门控 |
| `--no-trade` | 关闭执行器 | 只询价落盘 |
| `--goals-mode` / `--ft-mode` | `dry` \| `live` | 分通道覆盖 |

硬顶（`.env` 优先）：`QUOTE_MAX_USDC`（默认 1）、`QUOTE_MAX_SHARES`（默认 25）、`QUOTE_MAX_OPEN_USDC`（默认 1000）。

幂等：`event_key|token_id|trade`；成功 live 单重启不重复发。

---

## 快速开始

```bash
pip install -r .cursor/skills/polymarket-quote/requirements-trade.txt

# 仓库根目录 .env（切勿提交），至少包含：
#   PRIVATE_KEY / FUNDER / SIGNATURE_TYPE / CHAIN_ID / CLOB_HOST
#   QUOTE_DQD_STREAM_OBSERVE=1
#   PM_PROXY=http://127.0.0.1:1082   # 可选，默认常为此

python3 frontend/run_main.py --no-browser
# 浏览器打开 http://127.0.0.1:8790/
```

常用覆盖：

```bash
python3 frontend/run_main.py --take-depth walk --max-usdc 5 --no-browser
python3 frontend/run_main.py --goals-mode dry --ft-mode dry --no-browser   # 全 dry
python3 frontend/run_main.py --no-trade --no-browser                      # 只询价
```

停止：对 `run_main` 进程 `Ctrl-C` / `kill`，或 `POST http://127.0.0.1:8790/api/stop`。

### 上线检查清单

1. Hub：Quote 进程 up · Trade `goals:live ft:live` · Boards 4/4。  
2. Bridge 看板有配对场次。  
3. Pitch Gate（`:8791`）在进球后出现帧与 `play_state`。  
4. `data/pm-quote/watch.log` 可见：`pitch-gate → START` → `CONFIRM` / `IN_PLAY` / `VAR_VETO` / `TIMEOUT` / `CANCEL`。  
5. 成交写入 `data/pm-quote/trades.jsonl`（live 且成功时 `live: true`）。  
6. 回撤日志为取消门控/rest，**不应**期待自动 flatten。

---

## 关键环境变量

| 变量 | 默认 / 说明 |
|---|---|
| `QUOTE_DQD_STREAM_OBSERVE` | 须为 `1`，否则进球门控不可用 |
| `QUOTE_GATE_SOURCE` | `dom`（默认，读动画 DOM）/ `ocr`（旧截图+OCR 路径，代码保留） |
| `QUOTE_DOM_OPEN_TIMEOUT_S` | DOM 模式打开动画页的上限，默认 15 |
| `QUOTE_PITCH_STATE` | 仅 `QUOTE_GATE_SOURCE=ocr` 时需要 |
| `QUOTE_GOALS_MODE` / `QUOTE_FT_MODE` | 未设则均为 `live` |
| `QUOTE_MAX_USDC` / `QUOTE_MAX_SHARES` | 单笔硬顶（默认 1 / 25） |
| `QUOTE_MIN_BUY_PRICE` | 默认 0.6；**门控确认单跳过** |
| `QUOTE_GATE_PROTECT_S` | 门控买后保护窗口秒数，默认 300；`0` 关闭 |
| `QUOTE_NAMI_OBSERVE` | 默认 `0`；`1` 订阅纳米实时流落盘（observe-only，不影响下单） |
| `QUOTE_REST_ENABLED` | 默认 `0`；`1` 才挂 0.99 rest |
| `QUOTE_REST_EXPIRE_S` | rest 过期秒数，默认 3600 |
| `QUOTE_FT_MAX_AGE_S` | 默认 900，过旧事件跳过 |
| `QUOTE_INTERVAL` | watch 最大空闲间隔，默认 0.25s |
| `MAIN_BRIDGE_INPROC` | 默认开；`0` 则走 board 文件唤醒 |
| `PM_PROXY` | CLOB/Gamma 代理 |
| `PRIVATE_KEY` 等 | live 下单必填；勿提交 |

---

## 日志与数据

| 路径 | 用途 |
|---|---|
| http://127.0.0.1:8790/ | System Main 总控 |
| http://127.0.0.1:8791/ | Pitch Gate：按球逐帧状态；筛全部 / in_play / 回撤 |
| `data/pm-quote/watch.log` | 询价 / 门控 / 下单 stdout |
| `data/pm-quote/trades.jsonl` | dry / live 尝试 |
| `data/pm-quote/quotes.jsonl` | 完整询价包 |
| `data/bridge/events.jsonl` | 持久化 bridge 事件 |
| `data/bridge/matches.json` | 最近配对结果 |
| `data/pm-quote/dqd_stream_observe.jsonl` | 门控帧元数据 |
| `data/pm-quote/dqd_stream_frames/` | JPEG 截帧 |
| `data/pm-quote/pitch_state_judge.jsonl` | 每帧 OCR 判定（仅 `QUOTE_GATE_SOURCE=ocr`） |
| `data/pm-quote/nami_observe.jsonl` | 纳米实时流采样（比分 / 球位 / 原始 payload），仅调研 |
| `data/pm-quote/dom_vs_ocr.jsonl` | DOM 读数与 OCR 判定并排记录（仅 `QUOTE_GATE_SOURCE=ocr`） |
| `data/pm-quote/open_positions.json` | 未平 `buy_win` 仓 |
| `data/pm-quote/cursor.json` | 已处理 key / 终场 id |

热路径：DQD 轮询 → rematch → 内存 `event_queue` → pitch-gate → 一次 `/books` + 买。  
冷路径：`events.jsonl` / `trades.jsonl` / 截帧落盘（异步复盘）。

### 反复出现 `supervisor: quote watch restarted`

代表 `pm_quote.py watch` 启动失败退出，被 System Main 拉起。真正的原因只在 `data/pm-quote/watch.log`，最常见的是网络/代理抖动导致 CLOB 鉴权失败：

```
[py_clob_client_v2] request error: handshake operation timed out
trade setup failed: PolyApiException[status_code=400, {'error': 'Could not create api key'}]
```

现有缓解：`derive/create_api_key` 会重试 4 次（退避 1.5s 起），watch 启动阶段再整体重试 5 次（退避 5s 起、上限 30s），仍失败才退出；supervisor 对「启动后 120s 内退出」按 10s→20s→…→300s 指数退避，并打印退出码。若日志里持续是同一个鉴权错误，先查代理（`PM_PROXY`）与 `PRIVATE_KEY` / `FUNDER`，不要靠重启硬扛。

DQD 侧的 `ssl.SSLEOFError` traceback 不致命，bridge 线程会在 15s 后自行重试。

---

## 已知局限（运营须知）

以下为当前实现的真实边界，不是「预期内可忽视」的噪音：

1. **保护窗口只减损不免损**：回撤后价格已经在往下走，FAK 平仓通常拿不回全部本金；`entry×80%` 的最低卖价意味着极端行情下可能吃不掉，残量留到终场。  
2. **超窗回撤仍无保护**：默认 300s 之外的回撤依旧等终场，那时价格已归零，等于损失已实现。窗口设上限是为了避免被懂球帝的比分抖动误触发平仓。  
3. **已成交的买无法靠 cancel 撤回**：同 tick 内未 drain 的 `in_play` 会被 `cancel_match` 转为 `buy_revoked`；若上一 tick 已 FAK，只能靠保护窗口平仓。  
4. **VAR 依赖动画文案**：纳米不渲染 `VAR` 就不会 `var_veto`；买入之后出现的 VAR 本身不触发平仓，要等懂球帝真正回撤。  
5. **DOM 结构依赖**：`.pop-box` / `.center-box` 是纳米前端的私有实现，改名会让门控读不到文本 → `unclear` → 停止下单（安全方向，但会静默停交易）。  
6. **单帧确认更激进**：改回 1 帧后买入更快、买入率更高，快回撤的拦截完全交给保护窗口。  
7. **动画源是第三方且非公开接口**：纳米改动 URL 形态或页面结构会让门控退化为「读不到合格状态 → 超时不下单」（安全方向，但会静默停止交易）。同理 `animation_live` 缺失的场次（友谊赛、业余级别）没有门控能力。  
8. **卡死页面只能靠时钟识别**：判定要求 `.center-box` 时钟相对上次读数有推进，开页时会先取一次基线，所以第 0 帧也受保护。但若纳米某天不渲染时钟，这层保护会静默失效（比分相符仍是主要防线）。  
9. **纳米实时流尚未验证的部分**：`QUOTE_NAMI_OBSERVE` 只是采样落盘。protobuf 无 schema，字段靠 wire format 反推；采样期间没有收到过 `nft/zh` 主题消息，**VAR 事件能否从该流获取仍未证实**。  

更细的盘口结算与 API 见 `.cursor/skills/polymarket-quote/reference.md`。

---

## 静态检查

```bash
python3 -c "from pathlib import Path; import py_compile; \
  [py_compile.compile(str(p), doraise=True) for p in Path('.cursor/skills').rglob('*.py')] ; \
  [py_compile.compile(str(p), doraise=True) for p in Path('frontend').rglob('*.py')]"

python3 .cursor/skills/match-bridge/scripts/smoke_ft_period.py
python3 .cursor/skills/match-bridge/scripts/smoke_match_hardening.py
python3 .cursor/skills/polymarket-quote/scripts/smoke_trade_modes.py
python3 .cursor/skills/polymarket-quote/scripts/smoke_pitch_gate.py
python3 .cursor/skills/polymarket-quote/scripts/smoke_post_goal_sampler.py
```

调试单跑（日常勿与 System Main 并行）：

```bash
python3 .cursor/skills/polymarket-quote/scripts/pm_quote.py once --from-bridge --json
```

各模块说明：`.cursor/skills/*/SKILL.md`。

---

## 目录

```text
.cursor/skills/     # Agent skills（脚本 + SKILL.md + reference）
frontend/           # System Main hub + 各看板
data/               # 运行时快照 / jsonl（勿提交密钥与隐私）
```

## 安全

- 默认 goals+ft 均为 **live**；进球仍须过 pitch-gate。请用小额 `QUOTE_MAX_USDC` 起步。  
- 切勿提交 `.env`、私钥、`.idea` 等。  
- 门控依赖 `QUOTE_DQD_STREAM_OBSERVE=1`（`QUOTE_GATE_SOURCE=ocr` 时另需 `QUOTE_PITCH_STATE=1`）。  

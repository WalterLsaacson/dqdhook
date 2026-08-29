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
         ┌───────────┼───────────┬──────────────┐
         ▼           ▼           ▼              ▼
   进球 pitch-gate   终场立刻询价  回撤 AF flatten  进球 +10min 再扫
   （DOM∧AF 一刀）   （无门控）    （认分才卖）     （当时比分）
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
| Pitch Gate 看板 | `:8791` | 每球逐帧 DOM 状态 + AF 比分观察（可筛买入 / 无射门 / 回撤） |
| API-Football Bridge 看板 | `:8792` | DQD→AF fixture 缓存 / events |
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
   - `score_change` + 进球（比分上升）→ 启动 **pitch-gate**，并排队 **T+10** 再扫盘
   - `score_change` + 回撤（任一侧比分下降）→ 取消该场门控与 rest；**已有仓**才开 5s 观察，**AF 认回撤比分**再 flatten（T+10 排队不取消，到点用当时比分）
   - `match_finished`（`period` 切入 `FT`）→ 取消未到期 T+10 + rest；**立刻询价下单**（不过门控）
5. 加时中（DQD 仍在踢、`minute>90` 且 `injury_time==0`）的比分抖动**不发事件**，避免误触发。

当前会动 CLOB 的路径（只买 `buy_win`）：

| 策略 | 触发 | 是否过门控 | 金额 | 限价 rest |
|---|---|---|---|---|
| **Pitch-gate 进球** | 已配对进球，DOM `in_play` ∧ AF 认分 | 是 | `QUOTE_GOAL_MAX_USDC` | 仅当 `QUOTE_REST_ENABLED=1`（`QUOTE_REST_USDC`） |
| **Locked sweep** | 门控买时，该 token 在**上一分**已是 live WIN | 是（同一刀） | `QUOTE_LOCKED_SWEEP_USDC` | 同门控 rest |
| **T+10 再扫** | 进球后 10 分钟，按**当时比分** | 否 | `QUOTE_T10_USDC`（FAK 与 0.99 GTC 各用该金额） | **始终挂**（不看 `QUOTE_REST_ENABLED`） |
| **终场** | `match_finished` | 否 | `QUOTE_FT_MAX_USDC` | 不挂 |
| **终场灰尘盘** | 终场已锁定 WIN、ask≤0.01 | 否 | `QUOTE_FT_DUST_USDC` | 不挂 |

### 2. 进球通道（pitch-gate，核心）

对每一个已配对的 DQD 进球：

| 步骤 | 行为 |
|---|---|
| 启动 | `PitchGateCoordinator.start_gate`；**此时不下单** |
| 采样 | 进球后 **+0s** 起每 **5s** 先采 DOM；**本拍 `in_play` 才同帧打 AF**，直到 **120s** |
| 买条件 | **同帧** DOM `in_play` ∧ AF。射门不卡买入。Odds Grade A **只观察、不下单** |
| 下单 | watch tick **入队** CLOB worker（`trade_context.pitch_gate=true`），**每球最多一刀**；买完 **停 AF**（省额度），**DOM 继续抓到 120s** |
| VAR | 任一拍判为 **VAR** → 该球 **永久不下单**（`pitch_gate_var_veto`） |
| 超时 | 120s 内未对齐 → `pitch_gate_timeout`，不买 |
| 回撤 | 取消进球会话；**已有仓**才开 5s AF+DOM 观察；某一拍 **AF `score_match`**（回撤后比分）→ flatten 并**立刻停轨**；AF 不认 → **持仓** |

**Locked sweep（进球作废仍 WIN）：** 门控询价时，若 token 在事件 `prev` 已经是 live WIN（这球不算也锁死），FAK 吃光 ask≤0.995 的剩余卖盘，**不走** `QUOTE_GOAL_MAX_USDC`。金额顶 `QUOTE_LOCKED_SWEEP_USDC`（默认 $1000），仍受 `QUOTE_MAX_OPEN_USDC`。`QUOTE_LOCKED_SWEEP=0` 或金额 `0` 关闭。回撤后仓位在新比分仍 WIN 的（如 1-1→1-0 的主队 0.5 大球）**不平仓**。

买入侧 **AF∨DOM 或门否决、不实现**（见 `design-af-dom-or-gate.md`）。

**判定源：读动画 DOM，不截图、不跑 OCR**

动画自己就把状态渲染成文字和 CSS class。直接读：

| 读什么 | 例子 | 用途 |
|---|---|---|
| `.pop-box` 文本 | `皮尔利斯 危险进攻` / `VAR 回看` | 进行中 / 停止 关键词，与 OCR 用同一张关键词表 |
| `.center-box` 文本 | `78:57 1 : 0` | 比赛时钟 + 底部比分（**精确值**，不存在误读） |
| 状态 class | `possession-rect` / `dangerous-attack-move` | 看板展示；文案闪事件时的补充信息 |

- 共用 **一台 Chromium**：进行中已配对场预开 tracker 页，进球后第一拍只读 DOM；同场下一记进球复用标签。冷开页仍约 0.5–2s（仅未预热的场）。之后每次读数 **2–8ms**。
- 页面来源仍是 `match_list` 每行自带的 `animation_live` 直链（`tracker.namitiyu.com/...&id=<nami_id>`），用来读 DOM，**不做 MQTT 球位观察**。读不到时会回退去找懂球帝页面里的 `iframe.md-anim-iframe`。
- 15s（`QUOTE_DOM_OPEN_TIMEOUT_S`）内找不到动画 → `unavailable`，不下单。
- 不截图、不写 JPEG、不跑 OCR。

**门控买入条件（仅 AND）：**

- **AND**：DOM 进行中关键词 + 比分=期望 + 时钟在走 + 从未 VAR + 同拍 AF `ok && score_match`  

Odds Grade A 只写入观察 jsonl，**不触发买入**。回撤只认 AF 比分。  

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
| 开场球刚被同一过渡回撤过，或开场球时钟 ≥90′ | `pitch_gate_reversal_risk_skip`，不开闸（约 35′ 普通开场球仍买） |
| 懂球帝回撤该球（买入前） | 取消门控 + 撤销未 drain 的买信号 |
| 懂球帝回撤该球（已有仓） | 5s 轨等 **AF 认回撤比分**才 flatten，**认分后立刻停轨**；120s 不认 → 持仓 |
| 动画已改比分、随后 DQD 才长延迟回撤 | 买入时无法预知；出口靠回撤后再开 5s 轨 |
| VAR 出现在**已经下单之后** | 拦不住该刀（除非随后 DQD 回撤且 AF/DOM 认分）

### 3. 进球 +10 分钟再扫盘（T+10）

已配对进球一出现就排队（`data/pm-quote/t10_pending.json`），**不管 pitch-gate 最终买没买**。默认 **600s** 后按**当时**懂球帝比分再询价：

- 有 misprice → 同一套 `buy_win` FAK（fee / `min_net` / ask≤0.995；跳过 `min_buy_price`；**不做** locked sweep）
- **每个已锁定 WIN 的 token** 再挂一笔 **@0.99 GTC**（不依赖 `QUOTE_REST_ENABLED`）
- FAK 和限价**各**用 `QUOTE_T10_USDC`（叠，不是「一共这么多」）；rest **不受** `QUOTE_MAX_OPEN_USDC` 卡住
- 终场取消未到期任务并撤 rest；回撤不取消排队
- `QUOTE_T10_USDC` 未设或 `0`、或 `QUOTE_T10=0` → 关闭。到期超过 `QUOTE_T10_MAX_LATE_S`（默认 900s）的任务丢掉（进程挂太久会漏扫）

### 4. 终场通道（FT）

- `match_finished` 到达后**立即**按终场比分解读盘口、询价；**无** pitch-gate。
- 默认 **live**；同 `match_id` 终场只处理一次（`cursor.processed_ft_match_ids`）。
- 过旧事件同样受 `QUOTE_FT_MAX_AGE_S`（默认 900s）约束。

### 5. 回撤、买后保护与持仓

懂球帝回撤是**触发器**，不是立刻 flatten。已有仓才开 5s AF+DOM 确认轨：某一拍 **AF `ok && score_match`**（回撤后比分）→ flatten，**不受** 300s 保护窗限制，并**立刻停 AF+DOM**（与买入后 DOM 拖到 120s 不同）。DOM 中心比分（庆祝/VAR/僵死时钟）只记观察，**不单独卖出**。AF 报错或比分仍是进球前 → 不平仓。动画页打不开仍继续采 AF。120s AF 不认 → **持仓**。未买入的回撤只取消门控。

| 动作 | 行为 |
|---|---|
| DQD 回撤 | 取消 **rest** + **进行中的进球 pitch-gate** + 撤销未 drain 的买信号 |
| 已有仓 | 新开 5s AF+DOM+Odds；**AF 认回撤比分** → flatten → **停轨**；超时持仓 |
| 无仓 | 只取消门控，不开确认轨 |
| 未确认的回撤 / FT 持仓 | 仍受 `QUOTE_GATE_PROTECT_S`（默认 300s）；超窗或 `=0` 等终场 `ft_reversal_vs_entry` |
| 同场新进球 | 取消该场先前未完成的门控会话（`superseded_by_new_goal`） |

平仓卖出沿用既有风控：按 `entry×80%` 设最低卖价，不做 0.01 甩卖；吃不掉的残量留给后续 tick 重试。持仓记录在 `data/pm-quote/open_positions.json`，带 `pitch_gate` 与 `opened_at` 两个字段用于判定窗口。

Pitch Gate 看板：普通回撤为**橙色**；若该球曾判定过 `in_play` 后又回撤，列表按钮/徽章为**红色**（「回撤·曾in_play」）。回撤后的 AF/DOM 采样是独立的**青色**「回撤观察」卡片（比分变化如 `1-0→0-0`），与原进球卡可互跳对照。

### 6. 询价与成交规则

触发询价后（门控确认的进球，或终场）：

1. 用 bridge 的 `event_id` / `market_refs` + 预热缓存 `data/pm-quote/market_cache/{match_id}.json` 拉盘口定义。  
2. 按当前比分结算各 token：`WIN` / `LOSE` / `PENDING`。  
3. **只交易 `buy_win`**（买已锁定为 WIN 的一侧）；`sell_lose` 已关闭。  
4. CLOB：`POST /books` 批量吃盘；默认 **`walk`** 深度（受 `max_levels` / `max_usdc` / `max_shares` / `max_slippage` 约束），FAK 市价。  
5. 手续费模型：`fee ≈ feeRate × p × (1−p)`（默认 `feeRate=0.05`）；需 `net_edge ≥ min_net`（默认约 0.00475，对应 ask≤0.995）才算 misprice。  
6. **门控确认单和终场**都跳过 `min_buy_price`（默认 0.6）；门控还跳过部分 $1 尺寸地板。仍受 `QUOTE_GOAL_MAX_USDC` / `QUOTE_FT_MAX_USDC` / fee / `min_net` 约束。Locked sweep / T+10 见上表。  
7. 极端价（≤0.01 或 >0.995）默认跳过，除非 `--allow-extreme-prices`。**例外：** 终场已锁定 `WIN`、ask≤0.01 仍 FAK（`QUOTE_FT_DUST_FAK`，默认开），金额 **`QUOTE_FT_DUST_USDC`（默认 $100）**，独立于 `QUOTE_FT_MAX_USDC`，仍受开仓剩余额度限制；max_price 卡在足球 tick **0.01**，不把 0.001 幽灵墙当成可吃深度；没吃到不记仓。进球门控仍跳过 ≤0.01。  

**涵盖盘口（有则报）：** 胜平负六 token、大小球（含球队/半场）、BTTS、准确比分等（见 `polymarket-quote/reference.md`）。Live 进球 / T+10 只报**已经锁死**的 WIN（Over 已越过盘口、BTTS 双方已进、exact No 已不可能）。

**限价 rest：**

| 来源 | 开关 | 金额 | 说明 |
|---|---|---|---|
| Pitch-gate | `QUOTE_REST_ENABLED=1` | `QUOTE_REST_USDC`（默认 $5） | 门控 WIN 无法 FAK 时挂 @0.99 GTC |
| T+10 | `QUOTE_T10_USDC`>0 | 与 FAK 同变量 | **每个**已锁 WIN token 一笔；与 FAK 叠；不受开仓顶 |

足球 tick **0.01**（不信 0.001 元数据），0.995 向下收到 **0.99**。没有卖盘也挂。DQD 回撤 / 终场 / 手取消才撤；`QUOTE_REST_EXPIRE_S>0` 才改 GTD。

### 7. 模式与仓位

| 通道 | 默认 | 说明 |
|---|---|---|
| 进球 goals | **live** | 仍须过 pitch-gate 才买 |
| 终场 ft | **live** | 不过门控 |
| `--no-trade` | 关闭执行器 | 只询价落盘 |
| `--goals-mode` / `--ft-mode` | `dry` \| `live` | 分通道覆盖 |

硬顶（`.env` 优先）：`QUOTE_GOAL_MAX_USDC`（比分变化，默认回落到 `QUOTE_MAX_USDC`）、`QUOTE_FT_MAX_USDC`（终场）、`QUOTE_GOAL_MAX_SHARES` / `QUOTE_FT_MAX_SHARES`、`QUOTE_MAX_OPEN_USDC`（默认 1000）。

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
4. `data/pm-quote/watch.log` 可见：`pitch-gate → START` → `IN_PLAY` / `ALIGNED_BUY` / `WAIT_AF` / `VAR_VETO` / `NO_ALIGNED_BUY` / `CANCEL`；回撤还有 `OBSERVE START` / `FLATTEN_OR` / `OBSERVE_COMPLETE`；T+10 有 `t10 → SCAN` / `t10 → CANCELED`。  
5. 成交写入 `data/pm-quote/trades.jsonl`（live 且成功时 `live: true`）。  
6. 回撤立刻取消门控/rest；已有仓才开 5s 观察，**AF 认分**后 flatten 并停轨，否则持仓。  
7. 进球后约 10 分钟应再出现一次询价（`mode=t10_scan`），未到期任务在 `data/pm-quote/t10_pending.json`。

---

## 关键环境变量

| 变量 | 默认 / 说明 |
|---|---|
| `QUOTE_DQD_STREAM_OBSERVE` | 须为 `1`，否则进球门控不可用 |
| `QUOTE_DOM_POOL_MAX` | 共用 Chromium 标签上限，默认 24；满则踢最久未用的空闲页 |
| `QUOTE_DOM_WARM` | 默认开：进行中已配对场预开 tracker 页 |
| `QUOTE_DOM_WARM_INTERVAL_S` | 预热扫描间隔，默认 10s |
| `QUOTE_DOM_WARM_OPEN_TIMEOUT_S` | 预热开页上限，默认 3s（门控开页仍用 `QUOTE_DOM_OPEN_TIMEOUT_S`） |
| `QUOTE_DOM_OPEN_TIMEOUT_S` | 打开动画页的上限，默认 15 |
| `QUOTE_GOALS_MODE` / `QUOTE_FT_MODE` | 未设则均为 `live` |
| `QUOTE_GOAL_MAX_USDC` / `QUOTE_GOAL_MAX_SHARES` | 比分变化 / pitch-gate 单笔硬顶；缺省回落 `QUOTE_MAX_*` |
| `QUOTE_FT_MAX_USDC` / `QUOTE_FT_MAX_SHARES` | 终场单笔硬顶；缺省回落 `QUOTE_MAX_*`（股数默认按金额放大） |
| `QUOTE_FT_DUST_USDC` | 终场已锁定 WIN、ask≤0.01 的 FAK 金额，默认 **100**；`0` 关闭该路径 |
| `QUOTE_LOCKED_SWEEP` | 默认开：进球后若上一分已经 WIN，FAK 吃光 ask≤0.995；`0` 关闭 |
| `QUOTE_LOCKED_SWEEP_USDC` | 扫盘单笔金额顶，默认 **1000**；`0` 关闭该路径 |
| `QUOTE_T10_USDC` | 进球 +10 分钟再扫盘：FAK 与每 token 一笔 0.99 GTC **各**用该金额；未设或 `0` 关闭 |
| `QUOTE_T10_DELAY_S` | T+10 延迟秒数，默认 **600** |
| `QUOTE_T10_MAX_LATE_S` | 到期后最多晚多久仍扫，默认 **900**；超时丢任务 |
| `QUOTE_T10` | `0` 关闭 T+10（即使金额已设） |
| `QUOTE_GOAL_SIZE_TIERS` / `QUOTE_FT_SIZE_TIERS` | `ask:usdc`；终场不继承进球档，避免被 $50 卡住 |
| `QUOTE_MAX_USDC` / `QUOTE_MAX_SHARES` | 两通道都没设时的共享回落（默认 1 / 25） |
| `QUOTE_MIN_BUY_PRICE` | 默认 0.6；**门控和终场都跳过** |
| `QUOTE_GATE_PROTECT_S` | 门控买后保护窗口秒数，默认 300；`0` 关闭 |
| `QUOTE_REST_ENABLED` | `1` 才挂 0.99 rest（金额 `QUOTE_REST_USDC` 默认 $5） |
| `QUOTE_REST_EXPIRE_S` | rest 过期秒数；默认 **0 = GTC**（回撤/终场/手取消才撤） |
| `QUOTE_FT_MAX_AGE_S` | 默认 900，过旧事件跳过 |
| `QUOTE_INTERVAL` | watch 最大空闲间隔，默认 0.25s |
| `ODDS_API_IO_KEY` | 有则同拍写 Odds Grade（观察，不改 size）；开赛前 30 分钟采一次全盘口 |
| `QUOTE_PREMATCH_ODDS` | 默认开；`0` 关闭开赛前那一枪 |
| `QUOTE_PREMATCH_LEAD_S` | 触发提前量，默认 1800（开赛前 30 分钟采一次） |
| `MAIN_BRIDGE_INPROC` | 默认开；`0` 则走 board 文件唤醒 |
| `PM_PROXY` | CLOB/Gamma 代理 |
| `PRIVATE_KEY` 等 | live 下单必填；勿提交 |

---

## 日志与数据

| 路径 | 用途 |
|---|---|
| http://127.0.0.1:8790/ | System Main 总控 |
| http://127.0.0.1:8791/ | Pitch Gate：按球逐帧状态 + AF 比分观察；筛全部 / in_play / 回撤 |
| http://127.0.0.1:8792/ | API-Football Bridge：fixture 缓存 |
| `data/pm-quote/watch.log` | 询价 / 门控 / 下单 stdout |
| `data/pm-quote/trades.jsonl` | dry / live 尝试 |
| `data/pm-quote/t10_pending.json` | 进球 +10 分钟待扫盘任务 |
| `data/pm-quote/quotes.jsonl` | 完整询价包 |
| `data/bridge/events.jsonl` | 持久化 bridge 事件 |
| `data/bridge/matches.json` | 最近配对结果 |
| `data/pm-quote/dqd_stream_observe.jsonl` | 门控 DOM 读数（无 JPEG） |
| `data/pm-quote/af_observe.jsonl` | AF 比分（与 DOM 同一 5s/120s 节拍） |
| `data/pm-quote/book_context_observe.jsonl` | Odds/Bet365 Grade（观察） |
| `data/pm-quote/prematch_odds.jsonl` | 开赛前 30 分钟采一次的 Bet365+1xbet 全盘口 |
| `data/pm-quote/open_positions.json` | 未平 `buy_win` 仓 |
| `data/pm-quote/cursor.json` | 已处理 key / 终场 id |

热路径：DQD 轮询 → rematch → 内存 `event_queue` → pitch-gate → 一次 `/books` + 买。  
冷路径：`events.jsonl` / `trades.jsonl` 落盘（异步复盘）。

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
6. **单帧 AND 更严**：DOM `in_play` 单独不够，还要同拍 AF 认分；AF 限流会推迟/取消买入。  
7. **动画源是第三方且非公开接口**：纳米改动 URL 形态或页面结构会让门控退化为「读不到合格状态 → 超时不下单」（安全方向，但会静默停止交易）。同理 `animation_live` 缺失的场次（友谊赛、业余级别）没有门控能力。  
8. **卡死页面只能靠时钟识别**：判定要求 `.center-box` 时钟相对上次读数有推进，开页时会先取一次基线，所以第 0 帧也受保护。但若纳米某天不渲染时钟，这层保护会静默失效（比分相符仍是主要防线）。  
9. **T+10 rest 按 token 叠**：`QUOTE_T10_USDC` 是**每个**已锁 WIN 盘口的 FAK 上限，再另挂一笔同等金额 GTC；一场多盘、一晚多粒进球会叠。rest 不受 `QUOTE_MAX_OPEN_USDC` 卡住。  

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
python3 .cursor/skills/polymarket-quote/scripts/smoke_pitch_gate_dom.py
python3 .cursor/skills/polymarket-quote/scripts/smoke_dom_page_pool.py
python3 .cursor/skills/polymarket-quote/scripts/smoke_book_context_observe.py
python3 .cursor/skills/polymarket-quote/scripts/smoke_prematch_odds.py
python3 .cursor/skills/polymarket-quote/scripts/smoke_rest_ladder.py
python3 .cursor/skills/polymarket-quote/scripts/smoke_locked_sweep.py
python3 .cursor/skills/polymarket-quote/scripts/smoke_t10_scan.py
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

- 默认 goals+ft 均为 **live**；进球仍须过 pitch-gate。请用小额 `QUOTE_GOAL_MAX_USDC` / `QUOTE_FT_MAX_USDC` 起步。  
- 切勿提交 `.env`、私钥、`.idea` 等。  
- 门控依赖 `QUOTE_DQD_STREAM_OBSERVE=1`（`QUOTE_GATE_SOURCE=ocr` 时另需 `QUOTE_PITCH_STATE=1`）。  

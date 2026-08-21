# dqdhook

懂球帝 ↔ Polymarket 足球流水线：实时比分、赛事配对、截图门控后的 CLOB 询价与下单。

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
   （截图+OCR）      （无截图门控）  （不自动平仓）
```

| 模块 | 作用 |
|---|---|
| `dongqiudi-match` | 懂球帝赛程 / 比分 / 比分变化 |
| `polymarket-soccer` | Polymarket Gamma 足球对阵快照 |
| `match-bridge` | DQD ↔ PM 模糊配对；发出 `score_change` / `match_finished` |
| `pitch-state` | 动画截图 OCR：是否恢复比赛（`in_play` / `stopped` / `unclear`） |
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
| Pitch Gate 看板 | `:8791` | 每球截图 + 判定（可筛 in_play / 回撤） |
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
   - `match_finished`（`period` 切入 `FT`）→ **立刻询价下单**（无截图门控）
5. 加时中（DQD 仍在踢、`minute>90` 且 `injury_time==0`）的比分抖动**不发事件**，避免误触发。

### 2. 进球通道（pitch-gate，核心）

对每一个已配对的 DQD 进球：

| 步骤 | 行为 |
|---|---|
| 启动 | `PitchGateCoordinator.start_gate`；**此时不下单** |
| 抓帧 | 进球后 **+5s** 起第一帧，之后每 **5s** 一帧，直到 **150s** 超时或回撤取消 |
| 判定 | 每帧 JPEG → `pitch-state`：关键词（进攻/控球等）+ **底部比分 OCR 必须等于 DQD 期望比分** |
| 确认 | 需 **连续 2 帧** 均为合格 `in_play` 才发出一次买信号 |
| 下单 | `_quote_one`（`trade_context.pitch_gate=true`），**每球最多一刀**；买后仍继续抓帧直到超时（供看板复盘） |
| VAR | 抓帧过程中任一帧判为 **VAR** → 该球 **永久不下单**（`pitch_gate_var_veto`），仍抓满超时窗口 |
| 超时 | 从未达到两帧确认 → `pitch_gate_timeout`，不买 |
| 回撤 | DQD 回撤 → 取消会话 `pitch_gate_canceled`（若已买过，取消只影响剩余抓帧） |

**门控通过条件（须同时满足）：**

- OCR 命中「进行中」类关键词（进攻 / 控球 / 任意球等），且非 VAR/换人/庆祝等停止态  
- 底部比分 OCR = 该球期望的 `home_score-away_score`（比分未更新或不一致 → 不当作 `in_play`）  
- 连续 **2** 帧合格  
- 本会话**从未**出现过 VAR  

**明确不下单的情况：**

| 情况 | 结果 |
|---|---|
| 未配对 Polymarket | 不进门控 |
| 缺 `QUOTE_DQD_STREAM_OBSERVE` / `QUOTE_PITCH_STATE` | `pitch_gate_unavailable` |
| 事件过旧（默认 >900s） | `goal_stale` 跳过 |
| 比分 OCR 对不上 | 继续抓，不买 |
| 抓帧中出现 VAR | `var_veto`，该球不买 |
| 懂球帝回撤该球 | 取消门控；已成交不自动平仓 |
| 动画已改比分、随后 DQD 才长延迟回撤 | 比分门控拦不住；2 帧确认只能挡约一个采样间隔内的快回撤 |
| VAR 出现在**已经下单之后** | 拦不住该刀 |

### 3. 终场通道（FT）

- `match_finished` 到达后**立即**按终场比分解读盘口、询价；**无** pitch-gate。
- 默认 **live**；同 `match_id` 终场只处理一次（`cursor.processed_ft_match_ids`）。
- 过旧事件同样受 `QUOTE_FT_MAX_AGE_S`（默认 900s）约束。

### 4. 回撤与持仓

| 动作 | 行为 |
|---|---|
| DQD 回撤 | 取消该场 **rest 限价** + **进行中的 pitch-gate** |
| 自动平仓 | **不做**（进球回撤不 flatten；持仓留到终场相关逻辑或人工） |
| 同场新进球 | 会取消该场先前未完成的门控会话（`superseded_by_new_goal`） |

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
| 终场 ft | **live** | 无截图门控 |
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
#   QUOTE_PITCH_STATE=1
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
| `QUOTE_PITCH_STATE` | 须为 `1`，OCR 判定 |
| `QUOTE_GOALS_MODE` / `QUOTE_FT_MODE` | 未设则均为 `live` |
| `QUOTE_MAX_USDC` / `QUOTE_MAX_SHARES` | 单笔硬顶（默认 1 / 25） |
| `QUOTE_MIN_BUY_PRICE` | 默认 0.6；**门控确认单跳过** |
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
| http://127.0.0.1:8791/ | Pitch Gate：按球截图；筛全部 / in_play / 回撤 |
| `data/pm-quote/watch.log` | 询价 / 门控 / 下单 stdout |
| `data/pm-quote/trades.jsonl` | dry / live 尝试 |
| `data/pm-quote/quotes.jsonl` | 完整询价包 |
| `data/bridge/events.jsonl` | 持久化 bridge 事件 |
| `data/bridge/matches.json` | 最近配对结果 |
| `data/pm-quote/dqd_stream_observe.jsonl` | 门控帧元数据 |
| `data/pm-quote/dqd_stream_frames/` | JPEG 截帧 |
| `data/pm-quote/pitch_state_judge.jsonl` | 每帧判定 |
| `data/pm-quote/open_positions.json` | 未平 `buy_win` 仓 |
| `data/pm-quote/cursor.json` | 已处理 key / 终场 id |

热路径：DQD 轮询 → rematch → 内存 `event_queue` → pitch-gate → 一次 `/books` + 买。  
冷路径：`events.jsonl` / `trades.jsonl` / 截帧落盘（异步复盘）。

---

## 已知局限（运营须知）

以下为当前实现的真实边界，不是「预期内可忽视」的噪音：

1. **回撤不自动平仓**：进球被吹掉后，已成交 `buy_win` 仍持有，直到终场相关逻辑或人工处理。  
2. **已成交的买无法靠 cancel 撤回**：同 tick 内未 drain 的 `in_play` 会被 `cancel_match` 转为 `buy_revoked`；若上一 tick 已 FAK，回撤只能取消剩余门控/rest。  
3. **长延迟回撤**：动画板已显示进球、懂球帝十余秒后才回撤时，比分 OCR 与两帧确认都可能已放行。  
4. **VAR 依赖 OCR 文案**：未识别到 `VAR` 文本则不会 `var_veto`；VAR 出现在下单之后也不回滚。  
5. **时钟误当比分**：少数早期分钟时钟形如 `1:00` 可能被当成 `1-0`（OCR 启发式边界）。  
6. **截帧失败不重置确认计数**：两帧确认可能被失败帧「隔开」，略弱于「严格相邻两帧」。  

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
- 门控依赖 `QUOTE_DQD_STREAM_OBSERVE=1` 与 `QUOTE_PITCH_STATE=1`。  

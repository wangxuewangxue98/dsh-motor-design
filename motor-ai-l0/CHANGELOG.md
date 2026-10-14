# 更新日志

本项目的所有重大变更都记录在此文件中。
格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/)。

版本三重一致性：`package.json.version` = `SKILL.md` 的 `metadata.version` = `CHANGELOG.md` 最新条目。

## [0.5.3] - 2026-10-15 — L0 四项修复：截断可见化 / 冷却回退显式化 / 极数提示 / 密度自检

> **性质：缺陷修复（对齐 full rc.16 同款改动；l0 侧 P0-2 此前已有部分修复，本轮补齐其余三项）。**

1. **P0-2 截断可见化补强**：`MATRIX_TRUNCATED` 告警字段与 full 对齐
   （`requested_count` / `max_matrix_size` / `returned` / `suggested_max_matrix_size` /
   `hard_cap`），并明确提示「采样密度被静默改变 ⇒ Top1 效率与达标判定会随 count 漂移」。
2. **P1-1 冷却回退显式化**：`cooling` 不在合法值内时不再静默回退 `forced_air`，
   写 `COOLING_FALLBACK` 告警（含合法值列表），回退行为不变、可见性补齐。
3. **P0-4 极数提示**：未显式传 `poles` 且默认极数下可行组为 0 时，
   `runL0Pipeline` 输出 `pole_suggestion`（默认极数 / 建议补算极数 / 理由），
   不改默认行为，仅在输出侧提示用户「可尝试更多极数」。
4. **P0-1 密度自检**：新增 `density_check`（args 或 env `L0_DENSITY_CHECK=1`，默认 OFF），
   以 20 与当前 count 双密度跑 pipeline 比对 Top1 效率，`|Δ| < 0.1pp` 判已收敛；
   handoff 落 `density_check` 块。零回归：开关 OFF 时输出与 0.5.2 逐字节一致（探针 20/20 实证）。

- 新探针 `scripts/l0_fixes_probe_0.5.3.mjs`（20 断言，含默认 OFF 零回归对照）；
  `scripts/verify.mjs` 门禁补静态断言 + 运行时强断言（门内 spawn 探针）。
- 版本四处一致 0.5.3；verify **119/119**；探针 **20/20**；
  反向验证（破坏冷却判据）门禁双路变红，还原后全绿。

## [0.5.2] - 2026-10-15 — P0-门1' 修复：同步批量调用脱闸（同链判据改为调用链令牌）

> **性质：缺陷修复（安全相关；0.5.1 修复方案的缺陷转移）。** 0.5.1 把「同一调用链只扣
> 1 次」实现成「`_consumed` WeakSet（key = config 对象）+ `queueMicrotask` 复位」。
> 实测复现（`scripts/_repro_syncbatch.mjs`）：**同步 for 循环复用同一 config 引用 ×12 次，
> 整批只扣 1 次、12 次全部放行** —— 同步循环里微任务不穿插执行，第 2~N 次
> `alreadyConsumed()` 恒为 true。CI 脚本 / 压测循环正是这种写法，门禁形同虚设。
> 另注：0.5.1 探针 A 组每次新建 `{ handoffDir }` 字面量，恰好绕过该缺陷 ⇒ **假绿灯**。

### Fixed

- 🔴 **P0-门1'（同步批量脱闸）**：「同链只扣 1 次」的判据由 **config 对象引用**改为
  **显式调用链令牌** —— 新增 `newQuotaChain()` / `withQuotaChain(config, token)`；
  聚合入口 `runL0Pipeline` 每次调用新建唯一令牌，并派生带令牌的 `chainCfg` 透传给三个
  子纯函数。同链（同令牌）仍只扣 1 次；跨调用（哪怕同一 config 引用、同一 macrotask、
  同步循环）因令牌唯一 ⇒ **各计一次**，第 11 次起拦截。
- 🟢 **语义纠正**：不再把「一次用户动作」等价于「config 对象引用相同」——反向场景
  （同一 config 引用被复用于两次独立动作）此前也会被误判成同链、少扣一次。

### Tests

- `scripts/_repro_syncbatch.mjs`（新增，最小复现）：[A] 同 config + 同步循环 ×12 ——
  修复前 `blocked=0 / pass=12 / free=1`，修复后 `blocked=2 / pass=10 / free=10`；
  [A2] 新字面量 / [B] 每次 await 微任务 / [C] `freeQuotaEnabled:false` /
  [D] 整链 = 1 四种对照形态同测。
- `scripts/quota_probe_0.5.2.mjs`（新增）：新增 **A-same** 组（同一 config 引用 + 同步
  循环 ×12 必须同样 10 放行 + 2 拦且 `free=10`），补齐 0.5.1 探针漏掉的形态，22/22 通过。
- `scripts/verify.mjs` 门禁 H 升级：静态断言新增「必须导出 `newQuotaChain` /
  `withQuotaChain`」「禁止 `function alreadyConsumed(config)` / `markConsumed(config)`
  的 config 引用形态」「pipeline 必须把带令牌配置透传给三个子纯函数」；运行时自检新增
  8b「同一 config 引用 + 同步循环 ×3 = +3」。
  **反向验证**：把子调用改回 `buildParamMatrix(args, config)` 后门禁**双路变红**
  （静态断言 + 运行时「整链只扣 1 次」），还原后 117/117 通过。

## [0.5.1] - 2026-10-14 — P0-门1 修复：纯函数入口过闸 + 额度用尽引导补全

> **性质：缺陷修复（安全相关）。** 0.5.0 的闸门只装在四个 `registerX.execute` 上，
> `scripts/quota_probe_0.5.0.mjs` 实测复现：import 直调 `runL0Pipeline` ×12 次，
> 本地 `free-quota.json` 的 `free` 恒 = 0 —— 脚本/CI 可零限制跑完三步链。

### Fixed

- 🔴 **P0-门1（纯函数脱闸）**：四个纯函数入口全部注入 `assertFreeQuota` ——
  `runL0Pipeline`（`tools/l0/pipeline.mjs`）、`buildParamMatrix`（`param-matrix.mjs`）、
  `runDesignValidate`（`design-validate.mjs`）、`runL0Estimate`（`l0-estimate.mjs`）。
  与工具入口共用同一 `config` 引用，靠一次性消费标记保证**整链只扣 1 次**
  （探针新增防回归断言 F：一次 pipeline 调用 = +1，不会因加闸被扣成 4 次）。
- 🔴 **执行侧双重扣次隐患**：四个 `execute` 不再 `await enforceQuota(config)` 后直调纯函数
  —— `await` 边界会放跑一次性消费标记（microtask 重置先于 await 续体执行），
  导致每次工具调用扣 2 次。改为纯函数入口内聚闸门 + execute 捕获 `QuotaExceededError`
  直接回引导文案。
- 🟢 **`assertFreeQuota` 尊重 `freeQuotaEnabled: false` 开关**：纯函数入口与工具入口
  语义对齐；内部离线批量工具（`experience-hub capture` 2000 次迭代、`case-feedback`
  社区反馈聚合）据此显式 bypass，不再被 10 次/天上限卡死。

### Changed

- 🟢 **额度用尽引导文案补全**（用户诉求）：拦截提示现在包含 ——
  📮 联系插件制作者邮箱 `13546196530@163.com`（使用反馈 / 问题排查 / 购买授权 / 定制开发）；
  📦 下载完整版 `dsh-motor-ai` 的两条渠道（DSH 插件市场 dshhub-market 口令市场 /
  `npm install dsh-motor-ai@next`）；以及原有的「明天再用」选项。
- 🟢 **free-call 旁路遥测上报 `day` + `limit`**（P1 口径对齐）：随载荷附带本地自然日与
  本地限额，接收端按同一把尺计数，消「接收端 UTC+8 vs 插件本地时区」
  「接收端固定 10 vs 插件可覆盖」两个口径差（接收端同步支持，缺省回落旧行为）。

### Tests

- `scripts/quota_probe_0.5.0.mjs` → **`quota_probe_0.5.1.mjs`**（A 段从「脱闸复现」
  改为「闸门已捕获直调」）：**19/19 全绿** —— 直调 ×12 = 前 10 放行 + 第 11/12 拦、
  第 11/12 次抛 QuotaExceededError、本地 free=10、整链只扣 1 次、文案含邮箱+完整版引导。
- `scripts/_selftest_quota.mjs` 扩为 **22 项**：新增 P0-门1 三项（纯函数计配额 /
  整链单次/开关生效）+ 文案两项（联系邮箱 / 完整版下载引导）。**22/22 全绿**。
- `scripts/verify.mjs`：门禁 50 改写为「四工具纯函数入口 assertFreeQuota 接线 +
  execute 捕获 QuotaExceededError + 禁止 await enforceQuota 旧写法」静态断言；
  模块顶部新增配额隔离（`DSH_L0_QUOTA_FILE` 临时文件 + `DSH_FREE_DAILY_LIMIT=100000`）——
  否则跑一遍 verify 会扣开发者真实配额 ~17 次、跑两遍即假红灯。
- verify **117/117 全绿**。

### Docs

- README「免费额度与联网行为」整节同步：闸门覆盖范围（含纯函数直调）、
  用尽提示新文案、内部批量工具 bypass 说明。
- README「接收端部署」：官方接收端新增只读 `GET /api/dsh/stats` 聚合端点说明；
  `telemetry` / `experience` 两端点标注「契约预留、暂无插件客户端接线」。

## [0.5.0] - 2026-10-14 — 🔴 免费额度闸门语义升级：本地为准的硬闸 + 非阻塞旁路遥测

> **性质：行为变更（freeQuotaEnabled 默认开启时，免费档从「断网不限次」变为「每日 10 次硬闸」）。**
> 触发原因是一条实测到的事故级缺陷，而非功能偏好。

### 问题（实测证据）

`lib/quota.mjs` 的 `checkFreeQuota` 里是 `await fetch(...)` + **4s AbortController 超时**。
把 `commerceBackendUrl` 指向一个**在中国大陆不可达**的地址时（实测 `*.workers.dev` 被 DNS 污染：
同一域名两次解析分别返回 `103.252.115.49` / `199.59.149.236` + 伪造 IPv6，TCP 443 直连超时；
同网络下 `registry.npmjs.org` 正常），国内用户**每次工具调用都要白等 4 秒**才拿到 fail-open 放行
—— 限流没生效，延迟先炸了。

### Changed

- 🔴 **放行决策改为 100% 本地**：每日计数只认本地 `free-quota.json`（`$DSH_HOME/storages/dsh-motor-ai/`，
  与完整版 `lib/license.mjs:52` 的激活态同目录同文件 ⇒ **同机两端共用一本账**，不会分裂成两本）。
  本地自然日重置，`DSH_FREE_DAILY_LIMIT` 可覆盖上限（默认 10）。
- 🔴 **免费档 fail-closed**：用尽即抛 `QuotaExceededError`，**断网也拦**（旧版断网等于不限次）。
  这是「分发出去即失联」场景下闸门唯一能真正生效的方式。
- 🟢 **旁路遥测非阻塞**：`syncBackendTelemetry` 改为 fire-and-forget，调用链**不再 await**；
  超时定时器加 `unref()`，不把 CLI 进程多吊 4 秒。实测后端挂死时 `checkFreeQuota` **1ms 返回**、
  `enforceQuota` **完全不触网**（<50ms）。
- 🟢 **未配置后端 ⇒ 零网络请求**：不再向 `http://127.0.0.1:5000` 打无意义请求。
- 🟢 **后端不再决定放行**：即使后端返回 `allowed: false`，本地未超限即放行（**语义反转**，旧版会拦）。
- 🟢 **一次性消费标记**：同一调用链（同一 `config` 对象）只计 1 次，pipeline 内连调不重复扣；
  绕过工具网关直调内部函数同样被计。
- 新增导出：`assertFreeQuota` / `QuotaExceededError` / `getQuotaFilePath` / `getDailyLimit`。

### Tests

- `scripts/_selftest_quota.mjs` 重写为 **17 项**（断言方向随语义反转），全程用临时 `DSH_HOME` 隔离，
  绝不污染用户真实配额文件。新增两条**延迟回归**断言：后端挂死时 `checkFreeQuota` <300ms、
  `enforceQuota` <50ms。
- `scripts/verify.mjs` 门禁项 50~51 标题同步为「本地为准/开关/拦截/算法一致/非阻塞」。
- verify **117/117 全绿**；`_selftest_quota.mjs` 独立跑 **17/17 全绿**。

### Docs

- README「免费额度与联网行为」整节重写：新增 v0.5.0 语义升级说明与事故证据；
  「接收端部署」降级为**可选**（只影响数据回流与跨机额度归并，不再影响闸门是否生效）。
- 🔴 README/cordis.patch.yml 新增**中国大陆域名可达性实测**警示：`*.workers.dev`、`*.vercel.app`
  被 DNS 污染不可达；实测可达替代 `*.deno.dev` / `render.com` / `onrender.com` / `fly.io` / `railway.app`。
  建议国内部署者给 Worker 绑自定义域名。

## [0.4.4] - 2026-10-14 — 接收端配套 + 免费额度限额口径对齐

- **免费额度限额口径对齐**：`lib/quota.mjs` 引导文案回落值 `?? 5` → `?? 10`，与「每日 10 次」闸门决策一致。
- **接收端配置模板**：`cordis.patch.yml` 新增 `commerceBackendUrl` 注释示例；部署 `dsh-motor-ai-receiver` 后取消注释即让闸门生效。
- **文档**：README 新增「接收端部署（可选）」专节 + 免费额度区显眼化提示（feedback / 孤岛说明）。
- 配套接收端参考实现 `dsh-motor-ai-receiver`（Cloudflare Worker + D1）已就绪：3 端点 + 3 表，契约与 `lib/quota.mjs:17-19` 逐字一致。

## [0.4.3] - 2026-10-13 — 🔒 P0/P1 修复：免费额度开关显式化 + 权限声明 + dsh 兼容区间

> **性质：透明度修复 + 清单声明修正；默认行为仅一处可关（freeQuotaEnabled），其余零回归。**

### Fixed
- 🔴 **P0 透明度**：`cordis.patch.yml` 配置块新增 `freeQuotaEnabled`（默认 `true`），用户可在插件配置中
  显式关闭免费额度联网计数（此前该开关仅存在于 `lib/quota.mjs` 内部、未被暴露，用户无法关闭）。
  `README.md` 新增「免费额度与联网行为」一节，诚实披露：默认开启、`POST /api/dsh/free-call`、
  `device_id = sha256(hostname|platform|arch).slice(0,16)` 主机名指纹、后端不可达 fail-open、关闭方法。
- 🟠 **P1 权限声明**：`manifest.json` 的 `permissions` 由 `[]` 改为真实能力
  `["fs-read","fs-write","network","env","shell"]`（`shell` 来自随包发布的 `scripts/*.mjs` 中的 `child_process`）。
- 🟠 **P1 dsh 兼容区间**：`package.json` 的 `peerDependencies["@deepseek-ai/dsh"]` 与
  `dsh.compatibility.dsh` 由 `>=0.1.2 <0.2.0` 改为 `>=0.1.2 <0.3.0`，覆盖实测通过的 0.2.x 区间
  （dsh.so L5 实测于 0.2.0-rc.2 / 0.2.1-alpha.2），消除声明自相矛盾。

## [0.4.2] - 2026-10-13 — 🔧 包络字段补齐（矩阵行补 `torque_margin_rated`）

> **性质：字段补齐，无行为变更。**

### Fixed
- 矩阵行 `_physics` 此前漏带 `torque_margin_rated`（额定提供/需求），与 native 结果字段集不一致。
  现已补齐并加断言 `torque_margin_rated = 额定提供 / 需求`。verify **117/117 全绿**。

## [0.4.1] - 2026-10-13 — 🎯 转矩三档包络：需求 / 额定提供 / 最大提供 + 可行性判据

> **性质：新增判据字段 + 接线 + 清残留。默认行为零回归（过滤开关默认 OFF）。**

### Added
- **转矩包络**：矩阵每行 `_physics` 新增 `torque_capability`（额定提供）/ `torque_capability_max`（最大提供 = 额定 × K）/
  `overload_factor` / `torque_margin` / `torque_feasibility`，取代旧 `estimated_torque` 类比。
- **三档可行性判据** `torque_feasibility`：`ample` / `overload_only` / `undersized` / `unknown`。
- **过滤开关** `filter_undersized`（等价 env `DSH_L0_FILTER_UNDERSIZED`，**默认 OFF**）：
  开启后剔除「需求 > 最大提供」的几何偏小候选；全滤空则保留原矩阵并告警（不允许广筛静默归零）。
  过滤数量经 `undersized_filtered` 回吐，并产生 `UNDERSIZED_FILTERED` 显式告警。
- `torque_contract` 新增 `envelope_output` 段（三档语义、过载倍数取值与**未标定**声明、过滤开关名）。
- verify 新增 2 项（三档边界含等号 + 缺数据 unknown / 过滤开关 OFF 零回归 + ON 生效 + env 一致），**117/117 全绿**。

### Changed
- 核心依赖 `@wangxue98/dsh-motor-ai-core` `^1.1.0 → ^1.2.0`（包络常量与派生函数的真源）。

### Removed
- **旧 D²L 线性类比 `estimated_torque` 已删除**（`torque_nm × D²L/D²L_base`，同几何多转矩点误差 max 523%），
  连带移除 `torque_basis` / `torque_is_spec` / `torque_ratio_estimated_over_target` 三字段。
  此前「契约声明弃用但代码仍在产出」的两套口径并存隐患已消除。

## [0.4.0] - 2026-10-12 — 🛠 P0/P1/P2 广筛质量修复 + MC/CI 水位线

### Fixed
- **P0-1 VFD 低频断崖**：`LINE_FREQ_MIN_HZ` 20Hz → **5Hz**（`lib/motor-constants.mjs`）。
  旧口径把 VFD 恒转矩低频区（400kW 船推真实运行 12.5Hz/15Hz）整批判死（V18 全批 failed）；
  真正守门人 V16（n_sync=120f/p 须 > n）在低频下给出「选 X 极」可执行建议，不再静默放行。
- **P0-1 续：V16 同步边界 VFD 放宽**（core `design-rules.mjs`）：显式传 `line_freq_hz`（变频模式）时，
  同步转速落在 `[n·(1−SLIP_MIN_TOL), n]`（滑差 < 3%）属「临界可行」→ 降级为 **warning + 频率微调建议**
  （如 12.5Hz→12.9Hz 把 6 极@250rpm 从滑差 0 拉到 ~3%），不再整批判死；
  **工频（无 line_freq_hz，按 50Hz）维持严格口径**，4 极@1500rpm@50Hz 同步点断崖保留。
  直接效果：400kW/250rpm/6极@12.5Hz 船推的 **6 极设计不再被静默丢弃**，成为 Top1 推荐。
- **P0-3 极数候选可配置**：`poleCandidates(poles, opts)` 不再硬编码 ±2，支持
  `pole_span`（扩展跨度）/ `pole_candidates`（显式列表，非法值过滤）/ `poles_strict`（锁定单极）。
  默认 ±2 行为逐字节一致（零回归）。`buildParamMatrix` 透传 `pole_span/pole_candidates/poles_strict`。
- **P0-4 截断显式告警 + 硬上限**：`MATRIX_HARD_CAP=50000`；`maxMatrixSize` 越界钳断并告警；
  `count` 被 `maxMatrixSize` 夹断时回吐 `MATRIX_TRUNCATED` 告警（请求量/实际量/上限齐全），不再只埋 boolean。
  `index.mjs` 默认 `maxMatrixSize` 2000 → **5000**。
- **P0-2 极数透明度**：`buildParamMatrix` 回吐 `POLE_CANDIDATES` 告警，显式列出候选极数集与主极来源
  （user/recommended/explicit），解决「用户指定 6 极却静默生成 8 极」的无声越权。

### Added
- **P2 蒙特卡洛效率置信区间（MC/CI 水位线）**：`motor_l0_estimate` 新增 `mc_samples` / `mc_ci_level`
  入参（默认关闭）。开启后对 TopN 每行做 N 次 ±3% 参数扰动重算，统计效率分布，
  结果行附 `mc: {efficiency_mean, efficiency_ci:[lo,hi], efficiency_point, spread_pp, temp_rise_mean}`，
  返回体附 `mc_watermark` 提示「排序应结合 CI 水位线，点估计低于 ci_low 的方案在误差带下可能不达标」。

- **转矩双字段三级降级编排（P2/P1/P4/P5 报告落地）**：core 新增
  ① 典型 Bδ/A 知识表 `TYPICAL_B_DELTA`/`TYPICAL_SIGMA` + `queryTypicalBLoading`（按 induction/pmsm ×
  natural/forced_air/liquid_jacket 给中值+区间，σ 按定子内径口径校准 ~28kPa，与资料第五节一致）；
  ② 公式层 `computeTorqueD2L`（方法2 T=(π/2)·Bδ·A·D²·L·kw·kFe）/`computeTorqueStress`（方法3 T=(π/2)·D²·L·σ）；
  ③ 编排器 `l0TorqueEstimate`（期望输出=spec 9550·P/n 精确值；可达能力=surrogate(T1, env
  `DSH_L0_SURROGATE_TORQUE` 默认 OFF)→d2l(T2)→stress(T2b)→unavailable 恒 0 不伪造）；
  ④ `surrogate-engine` 去除 `torque:0` 硬置改走编排器；⑤ 结果行新增
  `torque_source/torque_confidence/torque_capability/capability_source/capability_confidence`，
  `torque_contract` 升级 `dual_field_resolver` 并显式弃用旧 D²L 线性类比（对拍误差 max 523%）。

### Changed
- core `@wangxue98/dsh-motor-ai-core` 同步升至 **1.1.0**（SLIP_MIN_TOL 常量、V16 VFD 放宽逻辑、
  典型 Bδ/A 知识表、转矩公式层 computeTorqueD2L/Stress、torque-resolver 三级降级编排器）。

## [0.3.3] - 2026-10-12 — 🔧 发布自检脚本 EBUSY 免疫（无功能变化）

### Fixed
- **`scripts/verify.mjs` 全面摆脱子进程 spawn 依赖**（本机 node 进程内 spawn 持续
  `EBUSY`，曾导致两项门禁无法执行）：
  1. 「进包 .mjs 相对 import 命中」门禁：由 `execFileSync('npm', ['pack','--dry-run','--json'], {shell:true})`
     改为**纯 Node 的 `package.json files` 白名单展开**（含 `!` 排除与 `*` glob）。
     语义等价——npm 在存在 `files` 时严格按白名单打包、`.npmignore` 不生效（见该文件首行注释），
     已用真实 `npm pack` 交叉验证展开结果一致（53 个进包文件）。
     顺带修复该门禁实现的两个 bug：`files` 项带尾斜杠（`tools/`）时路径拼出
     `tools//l0//x.mjs` 双斜杠、与 import 解析结果不匹配 ⇒ 误报缺包。
  2. 「配额闸门运行时行为」门禁：由 execFileSync 子进程改为 **ESM 顶层 await 预跑**
     （`_selftest_quota.mjs` 导出 `runQuotaSelfTest()`，verify.mjs 顶层 await 后同步读结果）。
     原因：verify 的 `check()` 是同步收集器（`const detail = fn()`），传 async fn 会静默假绿灯；
     子进程通道又受 EBUSY 影响。顶层 await 是既不骗人也不依赖 spawn 的唯一通道。
- `scripts/_selftest_quota.mjs` 重构为「导出函数 + 仅主模块自跑」双通道
  （`import.meta.url` 主模块判定），保留独立 CLI 能力（退出码 0/1）。

### 说明
- **无运行时功能变化**：0.3.2 的配额闸门功能完整；本版仅让发布自检脚本在
  spawn 受限环境（CI/本机故障态）下仍可执行，且消除一次真实误报风险。
- 版本三重：`package.json` = `SKILL.md metadata.version` = `CHANGELOG` 最新条目 = 0.3.3。

## [0.3.2] - 2026-10-12 — 🚦 免费额度闸门（L2 商业闭环 · 免费层落地）

### Added
- **每日免费额度闸门（G1）**：新增 `lib/quota.mjs`（`checkFreeQuota` / `enforceQuota` /
  `buildQuotaExceededMessage` / `getDeviceId`），四个 L0 工具
  （`motor_param_matrix` / `motor_l0_estimate` / `motor_design_validate` / `motor_l0_pipeline`）
  `execute` 首行接入。默认上限 5 次/天（后端 `DSH_FREE_DAILY_LIMIT` 可调）。
- **`lib/` 进入 files 白名单**：此前 `lib/` 不在 `package.json.files` 中，lib 模块不会进包——
  本次新增的 `lib/quota.mjs` 依赖其入包（否则四工具 import 必崩 `ERR_MODULE_NOT_FOUND`，
  与 0.3.0 事故同形态）。入包后 lib/ 共 12 个文件。
- **verify.mjs 新增 2 项门禁**：静态契约（导出 + 四工具接入 + device-id 算法锚定）+
  运行时行为（fail-open / 开关绕过 / `allowed===false` 拦截 / 算法一致 / 引导文案），
  后者走 ESM 顶层 await 预跑（check() 为同步收集器，async fn 会静默假绿灯；
  且不依赖子进程 spawn）。

### 设计约定（改动前必读）
- **断网 fail-open**：后端不可达一律放行，宁可少限一次不可错杀免费用户。
- **只有显式 `allowed===false` 才拦截**：后端任何异常/非预期响应按放行处理。
- **getDeviceId 算法与完整版 `lib/license.mjs:86` 逐字节一致**
  （`sha256(hostname|platform|arch).slice(0,16)`）——同一设备上 l0 与完整版
  必须是同一 device_id，否则后端免费额度账分裂成两本。改此处属事故级改动。
- 运维开关：`config.freeQuotaEnabled === false` 直接绕过闸门。
- 引导文案不含 `activate` 工具（l0 未提供），改为引导安装完整版 `dsh-motor-ai`。

## [0.3.1] - 2026-10-10 — 🐛 修复：DSH 启动时加载崩溃（缺 `lib/` 模块）

> **性质：缺陷修复 + 发布门禁补强。0.3.0 在 DSH Desktop 中激活即崩，导致 harness 无法启动。**

### 根因
- `index.mjs` 与 `tools/l0/l0-estimate.mjs` 残留 **3 处动态 `await import('./lib/...')`**（telemetry / experience-upload），指向本地 `lib/`。
- 抽 core 重构时静态 import 已改指 `@wangxue98/dsh-motor-ai-core`，但**动态 import 漏改**；而 `lib/` 不在 `files` 白名单 ⇒ 发布物不含 `lib/` ⇒ DSH 一加载即 `ERR_MODULE_NOT_FOUND`，required 插件激活失败。
- 放大器：`npm run verify` 在本地源跑（本地有 `lib/`），动态 import 可解析 ⇒ **103/103 假绿**；上一轮解包核验又只查 P0/版本/激活模块，漏了「入口引用的每个文件是否在包内」的完整性检查。

### 修复
- 3 处动态 import 全部 repoint 到 `@wangxue98/dsh-motor-ai-core`（单一真源，符合抽 core 初衷）。
- 补 core `index.mjs` 漏导出的 `reportTelemetry` / `reportExperience`（core 升至 `1.0.1`）。
- **新增发布门禁**：解包后校验「`index.mjs` 静态/动态 import 的每个相对路径文件必须存在于包内」，杜绝「发布面缺文件」类缺陷再次漏网。

### 影响
- 0.3.0 用户升级到 0.3.1 后 DSH 可正常激活；无功能/接口变更。

## [0.3.0] - 2026-10-10 — 📦 架构重构：抽取共享核心包 `@wangxue98/dsh-motor-ai-core`

> **性质：架构改进。为 L1/L2 插件扩展做准备，实现 L0/L1/L2 三层共享业务逻辑。**

### 变更内容
- 新建 `D:\dsh-motor-ai-core` 包，包含所有 lib/*.mjs 共享模块
- `dsh-motor-ai-l0` 改为依赖 core 包（`file:../dsh-motor-ai-core`）
- 迁移 14 个 .mjs 文件的 import 路径从相对路径改为 `@wangxue98/dsh-motor-ai-core`
- 版本升至 0.3.0

### 目标架构（两层次）
```
dsh-motor-ai-core     ← 共享核心库（L0/L1/L2 共用）
├── dsh-motor-ai-l0   ← 免费版（当前 L0 毫秒级预筛）
└── dsh-motor-ai      ← 完整版（未来：L1 RMxprt + L2 Motor-CAD）
```

## [0.2.10] - 2026-10-10 — 🔴 修复 design_exp 回流通道实际不可用（v0.2.9 双坑）

> **性质：紧急修复。v0.2.9 的 design_exp 脱敏回流通道在真实链路上是坏的，虽门禁 101/101 全绿。**
> 两个缺陷叠加 ⇒ 样本落盘后既发不出去、也留不住（静默失效，无任何报错）。

### 坑① `readPendingRecords` 未绑定到本地作用域 ⇒ 上报必崩
- 现象：`reportExperience()` 一执行就抛 `ReferenceError: readPendingRecords is not defined`。
- 根因：`lib/experience-upload.mjs` 里写的是
  `export { readPendingRecords } from './telemetry.mjs'` —— **re-export 只做转发，
  不会把符号绑定到本地作用域**。对外导出是好的（纯函数测试能过），
  但模块内部 `reportExperience` 直接调用时找不到该绑定。
- 修法：改为 `import { readPendingRecords } from './telemetry.mjs'` + `export { readPendingRecords }`
  （本地 import 与 re-export 成对，两边都保）。

### 坑② 样本缺 `ts` ⇒ 48h 滚动 GC 把全批当过期清空
- 现象：落盘 5 行后 `reportExperience` 恒返回 `{reported:false, reason:'no-pending'}`，
  且 `experience.jsonl` 被清空。
- 根因：`buildDesignExp` 未写 `ts`；`gcExperience` 按 `new Date(JSON.parse(l).ts || 0)`
  判定 ⇒ `ts=0` ⇒ 距今远超 48h ⇒ **全部判为过期删除**。
- 修法：`buildDesignExp` 产物必写 `ts: new Date().toISOString()`。

### 附带修复：样本 `plugin_version` 恒为空
- `l0-estimate` 调 `buildDesignExp(row, {})` 未传 meta ⇒ 服务端 `by_version` 画像
  全部落在 `""` 桶，无法按版本归因。
- 修法：`buildDesignExp` 增加兜底 —— 未显式传时读随包 `package.json.version`。

### 门禁补强（101 → **103/103**）
新增 2 条**时序/语义级**门禁，纯函数测试测不出这两类缺陷：
- `design_exp：样本必带 ts，且 48h GC 不得清空新样本`：真实跑
  落盘 → `gcExperience` → `readPendingRecords`（校验 kept/dropped/nextOffset）。
- `design_exp：readPendingRecords 必须在模块内有本地绑定`：**静态断言源码形态**
  （`import {...} from './telemetry.mjs'` 必存在且与 re-export 成对）——坑① 只能这样钉死。
另新增独立脚本 `scripts/verify_experience.mjs`（起临时 HTTP server 跑完整
落盘→GC→上报→收包链路，含脱敏校验），供 CI 单独调用。

**教训（通用，非本项目特有）**：
> 「每个函数单独测都过」≠「串起来能跑」。涉及**模块作用域绑定**与**时间/TTL 语义**
> 的缺陷，必须有一条把真实时序串起来的门禁；re-export（`export {x} from`）是
> ESM 的静默陷阱，对外可见但内部不可调用。

---

## [0.2.9] - 2026-10-10 — P3 消费回流（N3 版本放行 + export 训练集导出）

本批推进「设计样本脱敏回流方案 v1.0」的 **P3 消费回流**（P1 插件侧通道、P2 管理端
已分别在 0.2.8 与管理端落地）。P3 解决的是「回流样本怎么用起来」的最后一环。

### N3：代理模型版本白名单放行 3.x（P3 前置，回流闭环的最后一环）
- **阻塞背景**：经验回流触发重训后，产物 `version=3.1.0-fixseg`；而 JS 引擎
  `SUPPORTED_VERSION_PREFIXES=['1.','2.']` 只认 1./2. ⇒ 重训产物在
  `loadSurrogateModel` 直接抛「模型版本不支持」，**回流闭环断在最后一环**（N3）。
- **修复**：`lib/surrogate-engine.mjs` 白名单放行 `['1.','2.','3.']`，并导出常量供门禁断言。
- **安全性依据**：3.1.0-fixseg 与现役 2.3.0-fixseg **特征集一致**（实测 133,652 个
  系数逐字节比对 max diff = 0.0，仅 version 字符串不同），放行不改变任何推理行为。
  ⚠ 未来若引入非 fixseg 口径的 3.x（新特征集），须同步扩展推理侧兼容。
- `verify.mjs` 新增门禁（用临时模型文件走完整 `loadSurrogateModel` 实测）：
  3.1.0 可加载 **且** 未知版本 9.9.9 仍被拒（白名单不得退化成全放行）；门禁 **100 → 101/101**。

### 新增：`experience-hub export` 子命令（P3 训练集导出）
- **定位**：capture 样本是 L0 内部命名，重训管线（Python M1/M2/M3 + designs.db）
  要的是规整化 spec（power_kw/speed_rpm/voltage_v/poles/line_freq_hz/cooling）
  + 特征（stator_od/stator_id/core_length/poles/l0_eff）+ 标签（efficiency_raw）。
  `export` 做归一化，并**先报 N1 spec 完整率**、残缺行剔除且不静默导出。
- 用法：`node tools/feedback/experience-hub.mjs export --in <captured jsonl> [--out <jsonl>]`

### N1 修复验证（实证，非推断）
| 批次 | 行数 | 导出训练样本 | spec 完整率 |
|---|---|---|---|
| 修复前（0.2.7，10-09 采集） | 80000 | **0** | **0.0%**（全部剔除） |
| 修复后（0.2.9 重采） | 4000 | 4000 | **100.0%** |

这坐实了 `designs.db.l0_residuals` spec 覆盖率长期只有 ~2% 的根因：
不是「用户没填规格」，而是 **capture 采集侧字段错位把工况静默丢了**。

### 遗留（如实披露）：designs.db 历史 spec 无法库内回灌
`l0_residuals` 8634 行中 power_kw 仅 187 (2%)。已验证**三条路径全部不可行**：
  1. 同 `project_id`/`round_index` 批次内传播 → 可回灌 **0 行**（7 个已知组均不含缺失行）；
  2. 关联 `designs` 表（161 行含完整 spec）→ **0 行**（其 `project_id` 全为空，无关联键）；
  3. `signature` 仅含几何 JSON，不含 spec，无法反解工况。
**结论**：历史 8634 行的 spec 只能靠外部源匹配；而**新采集已 100% 带工况**（见上表），
即该缺陷自 0.2.9 起不再新增 —— 历史数据的补全留作 P3 后续项，不假装已修复。

## [0.2.8] - 2026-10-09 — 设计经验脱敏回传通道（design_exp）+ N1 采集修复

本批推进「设计样本脱敏回流方案 v1.0」的 **P1 插件侧**（设计定稿见
`MotorDesign/output/l0-plugin-audit/设计样本脱敏回流方案_v1.0.md`）。
外部 55 类真实调用只回传「使用统计」、不回传「实际设计了哪些电机」，
本批补上第二条并行通道，且**默认全关、显式 opt-in、零身份信息**。

### 新增：design_exp 设计经验脱敏回传通道（P1，默认关闭）
- **动机**：使用遥测健全（55 调用/0 失败），但外部设计分布不回流；本机只有
  1 份手动 capture 样本（12000 行，10 天未更新）。design_exp 把外部真实设计
  的「规格+几何+结论+误差带」脱敏回流，喂 L0 代理重训 / 真值库 / 可行性画像。
- **落地**：
  - `lib/experience-upload.mjs`（新）：`buildDesignExp` 从 L0 结果行裁剪
    `design_exp` record（spec/geo/out 三段 + rec_id 幂等键 + spec_complete N1 自检）；
    独立脱敏白名单 `DESIGN_EXP_*_FIELDS` + 红线黑名单 `DESIGN_EXP_FORBIDDEN`
    （账号/client_id/路径/提示词/密钥，与「本机绝对路径泄漏」门禁同形态反向验证）；
    本地 `experience.jsonl` + 字节 offset 增量（复用 telemetry `readPendingRecords`）
    + 48h 滚动 GC + 单批上限 `experienceBatchSize`(默认 200)；`reportExperience`
    fire-and-forget，失败不推进 offset、下批重报，**绝不影响工具主流程**。
  - `index.mjs`：Config 新增三开关 `designExperienceUpload`(默认 false) /
    `experienceEndpoint`(空则回退 telemetryEndpoint) / `experienceBatchSize`；
    新增 `setupExperienceUpload` 生命周期（周期 flush + 退出 flush，与 setupTelemetry
    同节奏、独立开关），启动日志加「设计经验回传 on/off」可见性。
  - `tools/l0/l0-estimate.mjs`：`execute` 成功分支对返回 TopN 裁 design_exp 落本地
    （开关关 → `getExperienceSink` 返回 null → 静默，零副作用）。
- **合规底线**：与 `telemetryEnabled`/`TELEM_FIELDS` 语义完全隔离（使用统计保持
  「无设计内容」不变）；默认全关（`designExperienceUpload=false` 且端点空 = 不启用）；
  规格与几何属可公开设计空间，可回流；身份信息经白名单硬编码排除 + 门禁断言双保险。

### N1 修复：experience-hub capture 字段错位（样本 0% 带工况的根因）
- **现象**：`designs.db.l0_residuals` spec 覆盖率仅 ~2%（真阻塞 v3.1 对齐重训）。
- **根因**：`cmdCapture` 从矩阵行读 `p.speed_rpm`/`p.voltage_v`（spec 口径），
  但矩阵行是 L1 口径命名 `speed`/`voltage`（param-matrix.mjs:310-312），
  两键恒 undefined，`JSON.stringify` 静默丢弃 ⇒ 样本 0% 带工况、无法复现。
- **修复**：`experience-hub.mjs` 的 `cmdCapture` 改从 `p.speed`/`p.voltage` 取值
  （键名保持 spec 口径 `speed_rpm`/`voltage_v`，与 design_exp 契约一致），
  连带修复 `cmdPair`/`cmdShadow` 对 `speed_rpm` 的消费。

### 验证
- `scripts/verify.mjs` 新增 4 条 design_exp 门禁（N1 完整率+降级 / 红线键注入拦截 /
  rec_id 幂等 / 单条体量护栏）：门禁 **96 → 100/100**；`node --test` 单测 22/22 无回归。
- **N3（放行 3.x 版本前缀）未在本批实施**：`surrogate-engine.mjs`
  `SUPPORTED_VERSION_PREFIXES=['1.','2.']` 仍拒 3.1.0-fixseg。它属行为改动
  （3.x 模型特征集可能与 2.x 不同），归 P3 消费回流阶段单独验证，本批保持零行为变化。
- 版本一致性修复：`manifest.json` 自 0.2.6 起漏更（0.2.7 未同步），本批对齐到 0.2.8。

## [0.2.7] - 2026-10-08 — P1 收口（pipeline 计数透传 + 升级注意 + 置信度修正）

本批修复源于 v0.2.6 的 1000 案例 + 10 万案例独立压测复盘（外部测试者），均为 P1 级、不阻塞 0.2.6 集成。

### P1-1：`motor_l0_pipeline` 透传 `returned_feasible_count`（补双通道分叉盲区）
- **现象**：v0.2.6 在 `l0-estimate` 通道补了 `returned_feasible_count`（返回集 TopN 内真正可行的数量，
  用于自检「返回集混入 infeasible_thermal 三态行」），但 **`pipeline` 通道（`motor_l0_pipeline`
  返回体与 handoff）只透传 `feasible_count`、丢弃 `returned_feasible_count`**。MotorAI_Client 的
  实际入口正是 `motor_l0_pipeline` —— 10 万案例压测中 82,103/82,103 该字段缺失。
- **根因**：作者门禁（verify.mjs）只断言 `l0-estimate` 通道的 `returned_feasible_count`，
  未覆盖 `pipeline` 返回体，形成「双通道分叉盲区」；CHANGELOG 0.2.6 称「两个通道均补」与现状不符。
- **修复**：
  - `tools/l0/pipeline.mjs` 返回体 summary 与 handoff summary 各补一行
    `returned_feasible_count: estimate.returned_feasible_count`
  - `scripts/verify.mjs` 新增门禁断言：运行 `runL0Pipeline` 并校验 `summary` 与 `handoff.summary`
    的 `returned_feasible_count` 均为 number≥0 且 ≤ `feasible_count`；门禁 95→**96/96**
  - CHANGELOG 0.2.6 的「两个通道均补」声明已订正为「l0-estimate 携带、pipeline 于 0.2.7 补齐」

### P1-2：损耗标定升级影响文档化 + `--cmp` 第五组 `thermal_recalc`
- **现象**：v0.2.6 新增分段铜损标定 `COPPER_LOSS_K`（small 0.2629 / medium 0.3949 / large 1.0039）
  后，同几何铜损 7117→13125W、温升 58.3→107.6K、效率 96→94.5，14 例旧可行变热不可行 ——
  属**物理修正（可行口径收紧），非回退**（逐例复核确认），但 CHANGELOG 缺「升级注意」，
  且 `--cmp` 四组分类无法表达这种「校验层两版都可行、估计层热可行收紧」的 case。
- **升级注意（下游消费方必读）**：升级到 0.2.6/0.2.7 后，预期会出现**更多 `infeasible_thermal`
  结果**（温升超限被拒），这是损耗标定修正后的正确行为，不是回归；可行率可能下降约 5.97%，
  ΔT 中位约 67.8K。请勿将此类变化误读为「新版本做不出方案」。
- **修复**：`scripts/cmp.mjs` 新增第五组 `thermal_recalc` —— 校验层两版都可行、但新版估计层
  因损耗标定出现 `infeasible_thermal` 而旧版可行的 spec 归入此组，**明确非回退**，不触发 CI 非零退出；
  估计层判定依赖公式通道 `verdict`，代理可用时保守降级为 `both_ok`（不会误报回退）。

### P1-3：公式通道 `confidence` 不再恒 1 + README 说明
- **现象**：公式通道（未标定默认通道）结果硬编码 `confidence: 1.0`，会误导下游把未标定公式输出
  当成高置信标定预测采信绝对值。
- **修复**：`lib/formula-engine.mjs` 公式通道 `confidence` 改为显式标记值 **0.5**
  （`FORMULA_CHANNEL_CONFIDENCE`），`buildL0Result` 默认值同步；README「两条通道的分工」补注：
  代理通道可能离线/覆盖率 0 而静默降级公式通道、公式 `confidence=0.5` 仅代表「未标定可排序不可绝对值采信」、
  经验回灌 `shadow` 显著更优即视为触发正式重训的信号。
- 代理通道仍输出逐预测 `confidence`（低于 `surrogateConfidenceThreshold` 自动降级），不受影响。

### 附带：遥测 `plugin_version` 修复 + 回归基线重定
- `index.mjs`：遥测上报的 `plugin_version` 改读插件自身 `package.json` 的 `version`
  （`config.pluginVersion` 不在 Config Schema 中，解析后恒 undefined，导致上报版本长期为空，P2 缺陷）；
  解析失败安全回落空串。
- `benchmarks/baseline.json`：损耗标定 v3.1 + 效率钳位与排序解耦后，
  `regression.mjs --update-baseline` 重定 6 案例基线（旧基线为 2026-09-23 口径，
  差异属预期口径变化而非回归；`small55kw` 的 thermal DEBT 如实保留）。

## [0.2.6] - 2026-10-08 — P0 motor_type 透传修复 + 经验回灌闭环 + --cmp 跨版本对比进 CI

本批修复源于 v0.2.5 的 1000 案例 + 10 万案例两轮独立压测（外部测试者复核，
含 4 次断言口径自我修正的完整留痕）。压测确认 0.2.5 的 7 项声明修复全部兑现、
V16 断崖在 1rpm 精度下彻底消除，唯一未修的 P0 在本版闭环。

### 发版卫生：泄漏门禁自身失效（0.2.5 tarball 遗留问题的收口）
- **门禁漏洞**：`P0 发布物无本机绝对路径泄漏` 的正则只匹配 `C:/Users/...`，
  对 `D:/MotorDesign/...` 这类工作区盘符**完全失明** —— 0.2.5 泄漏就是因此漏网，
  而门禁当时仍报 95/95 全绿。已扩展为匹配任意盘符 + 真实目录名
  （`C:/Users`、`D:/MotorDesign`、`D:/dsh-motor-ai-l0`），并用注入样本双向验证
  （注入 → ❌ 失败，移除 → ✅ 通过），非"永远绿"断言。
- **本次拦下的真实泄漏**：`scripts/loss_calib_v{2,3}_result.json` 为标定运行产物，
  v2 内含 `D:/MotorDesign/output/...` 数据集绝对路径。已删除，
  并在 `package.json.files` 加反选 `!scripts/loss_calib*_result.json`。
  **再次印证根因**：`files` 是白名单且优先级高于 `.gitignore`/`.npmignore`，
  「已 gitignore」≠「不进包」——本版起所有本地运行产物一律用 `!` 反选排除。

### P0：`params.motor_type` 透传（唯一阻塞零摩擦集成的缺陷）
- **现象**：79.5% 生成行 `motor_type` 缺失 → 代理通道 `detectMotorType()` 落入
  OD 启发式（od<400→pmsm），与生成路径默认（induction）矛盾，
  OD<612.4mm 中小型异步机被静默误判 pmsm → 分族选段失败 → skipped 19.8%
- **修复**（三处）：
  - `lib/motor-constants.mjs` 新增 `resolveMotorType()`：别名归一（补 'async'/'im' 等
    异步别名——它们此前不在任何别名表，是压测之外的第二处静默误判源）+
    缺省显式判 induction 并标 `assumed`
  - `tools/l0/param-matrix.mjs`：行对象**无条件**携带规范化 `motor_type`（与 0.2.5
    `line_freq_hz` 同模式）；`spec` 回吐 `motor_type_assumed`；缺省/无法识别时回吐
    `MOTOR_TYPE_ASSUMED` 告警（info/warning 两级）
  - `lib/surrogate-engine.mjs`：`detectMotorType()` **删除 OD 启发式**，
    改走 `resolveMotorType` —— 几何按异步口径生成时，推理端猜 pmsm 是放大错误不是修正错误
- P1 顺带：`l0-estimate` 通道 summary 补 `returned_feasible_count`（返回集可能混入
  infeasible_thermal 三态行，调用方需要可自检的口径）。**`pipeline` 通道透传在 0.2.7 补齐**——
  0.2.6 仅 `l0-estimate` 携带，MotorAI_Client 实际入口 `motor_l0_pipeline` 的返回体仍丢弃该字段
  （详见 0.2.7「P1-1」）
- 门禁 94→**95/95**：新增「P0 motor_type 透传」4 类断言（行级透传/双层告警/别名归一/P1 计数）

### 经验回灌闭环（tools/feedback/experience-hub.mjs，新增）
- `capture`：规格采样 → 公式通道（v3.1 已标定损耗）→ 本地 JSONL 经验样本
  （`models/experience/`，不出网不外发）
- `pair`：现役代理模型 vs 公式通道逐例配对，输出 MAE/bias/跳过原因分布
- `shadow`：确定性二分训练/评估集拟合影子分桶模型（不落盘覆盖现役模型），
  MAE 显著更优才建议触发正式重训
- **首轮实测（300 spec / 12000 样本）**：现役模型（旧损耗常数训练）MAE=7.9pt /
  bias=+5.8pt，影子基线 0.886pt —— 结论：建议触发正式重训（导出样本给 Python 侧 M1/M2/M3 管线）

### --cmp 跨版本对比（scripts/cmp.mjs，新增）
- 按「新规则是否拦截了旧规则放行的输入」四组归类：
  `both_ok / new_only（改进）/ old_only_blocked_by_rule（预期拦截）/ regression_suspect`
- **只有 regression_suspect 才判回退** —— 直接固化压测复盘教训
  （把 V18 拦截越界频率误读成「200rpm 回退 80pp」的事不再发生）
- CI 固化见 `.github/workflows/ci.yml`

### CI（.github/workflows/ci.yml，新增）
- `gate`：push/PR 跑单测 + 95 项门禁
- `cmp`：手动/每周定时，`scripts/cmp.mjs --against <npm latest>`，疑似回退退出非零

### 门禁与回归
- 门禁 95/95，单测 22/22
- `scripts/cmp.mjs --against 0.2.5` 实测：24 用例 0 疑似回退（P0 修复不改变可行性口径）

## [0.2.5] - 2026-10-08 — 10 万案例压测复核修复：V16 频率可配置 + 绝缘等级透传 + 转矩口径契约 + 极数合法性

本批修复源于 10 万案例规模压测的复核结论。
**压测报告的三处结论经源码取证后订正**，修复的是「判据与契约」而非「计算公式」。

### 社区反馈闭环（v0.2.5 新增）

新增 `tools/feedback/case-feedback.mjs`：用户在自己的机器上生成**结论式** issue 草稿。

| 层次 | 例子 | 是否回传 |
|---|---|---|
| L1 明细 | `stator_od=425, l0_eff=93.73` | ❌ 永不 |
| L2 结论 | `1400~1500rpm 可行率 66.7% → 1500~3000rpm 33.3%` | ✅ |
| L3 元统计 | `V16 failed×40` | ✅ |

三条硬约束：
1. **工具只返回草稿字符串**，不导出 `params`/`matrix` —— 脱敏靠"不产出"而非过滤。
2. 草稿强制过 **10 项脱敏自检**（几何/槽数/匝数/电流/效率/客户名/绝对路径/UNC/哈希/邮箱），
   命中任一即抛错拒绝输出。实测注入 10/10 全部拦截。
3. **永不实现自动提交**（`caseReportAutoSubmit` 恒 false）—— 自动上报是信任的重灾区。

断崖检测用双条件（绝对跌幅 ≥30pt **且** 相对比 ≤0.60），单用任一都会误判。

同期新增 `.github/ISSUE_TEMPLATE/` 三套模板（问题 / 案例反馈 / 标定数据）与 README 社区反馈小节。

### 损耗标定：未达阈值，拒绝写回（诚实记录）

用 MotorDesign 生产库 44 行 RMxprt 真值（`source_path LIKE 'cal_%'`，已剔除铁损占比 >50%
的失真行）标定 `formula-engine.mjs` 的损耗公式，**结果未达规格阈值（R²≥0.85/0.80/0.85），
故未修改任何常量**。根因已定位，属数据形态问题而非系数问题：

| 目标 | 实测 R² | 阈值 |
|---|---|---|
| 铜损 | 0.107 | 0.85 |
| 铁损 | 0.685 | 0.80 |
| 总损 | 0.296 | 0.85 |

- 铜耗：L0 反推匝数与 RMxprt 实际匝数高度相关（R²=0.962），但**铜损比值跨 420 倍
  （0.025~10.5）** ⇒ 非比例偏差。L0 输入是规格级（功率/电压/转速/极数），
  真值铜损是设计级（含绕组/并联/线规），**信息量不匹配**，乘性系数无法弥补。
- 铁损：两个基函数 `f·B^α` 与 `f²·B²` 在样本域内共线，最小二乘解出**负的 `ke`**
  （物理上无意义）⇒ 无法独立辨识。
- 正确的数据采集形态应是**控制变量扫参**（固定工况档，只变线规/并联/匝数之一），
  而非跨功率档随机抽样。后者恰是本批数据的形态。

标定脚本 `scripts/calibrate_losses.mjs` 已可复现运行（`--apply` 仅在全绿时写回）。

| 项 | 压测报告结论 | 源码复核 | 处置 |
|---|---|---|---|
| 转矩守恒 91.4% 失效 | P0 | ❌ **断言口径错**：`estimated_torque` 是 D²L 类比外推量，与 `target_torque`(9550P/n) 本就不同物理量；`native.torque` 与规格偏差 **0%** | 标记口径，不改公式 |
| B 级温升越限 806 例 | P0 | ✅ 成立，但根因是**参数透传断裂**（非判据缺失）：`params.insulation_class` 从未被消费 | 3 处透传 |
| 奇数极放行 297 例 | P0 | ⚠️ 当前版本实际已被 V14 拦；真实缺口是 V06 缺奇偶判据 | 补判据 |
| 1500rpm 断崖 | P0 | ✅ 成立，根因是 `LINE_FREQ_HZ=50` 硬编码 **+ `recommendPoles` 把 1460~1500 全给 4 极** | 频率可配置 |

### 1️⃣ V16 电源频率可配置 + 新增 V18（断崖根除）
- `motor-constants.mjs` 新增 `DEFAULT_LINE_FREQ_HZ=50` / `LINE_FREQ_MIN_HZ=20` / `LINE_FREQ_MAX_HZ=400` / `resolveLineFreqHz()`。
- `V16` 不再硬编码 50Hz；4极@1500rpm 在 50Hz 判死（n_sync=1500 触及同步点）、**60Hz 下合法**（n_sync=1800）。
- **新增 V18 电源频率合法性**（failed）：越界或非数值显式判出，不静默回落 50Hz —— 否则 V16 会拿错误频率去判超同步。
- `recommendPoles(speedRpm, lineFreqHz)` 改为按 `n_sync = 120·f/p > n` 反推档位：
  **1500rpm@50Hz → 2 极**（4/6/8 极的 n_sync=1500/1000/750 全部 ≤1500 即超同步），1460rpm 仍 4 极。
- **实测断崖消除**：修复前 1500rpm 可用率 **0.15%** → 修复后 **100%**（1440~1800rpm 全档 100%）。

### 2️⃣ 绝缘等级透传修复（B 级用户此前被按 F 级考核）
- 取证根因：`formula-engine.mjs` 只从 `opts.insulationClass` 取值，而 `l0-estimate.mjs` 两处调用**只传 `{efficiencyCap, maxTempClamp}`**
  ⇒ 矩阵行的 `insulation_class` 100% 丢失，B 级(80K) 用户一律按 F 级(105K) 判且无任何告警。
- 现取值优先级：**行内 `params.insulation_class` > 工具入参 > config > 默认 F**；矩阵行新增携带 `line_freq_hz` / `insulation_class`。
- 新增 `insulation_class_valid` / `insulation_class_effective`：非法等级（如 Z 级）显式标 `valid=false`，不再静默按 F 级放行。
- **实测**：30kW/1460rpm/natural ⇒ B 级可用率 95%（limit 80K）、F 级 100%（105K）、H 级 100%（125K），三档正确区分。

### 3️⃣ 转矩口径契约（P0，防下游误用）
- 矩阵行新增 `torque_basis='analogy_d2l'` / `torque_is_spec=false` / `torque_ratio_estimated_over_target`。
- `motor_l0_estimate` 返回体新增 `torque_contract` 声明：指明**规格字段为 `torque_nm`(9550P/n)**，
  `estimated_torque` 是 `torque_nm × (D²L)/(D²L_base)` 的几何类比外推量，**不得当规格使用**。
- 转矩公式本身**未改动**（`native.torque` 与规格偏差 0%）。

### 4️⃣ V06 补极数合法性（防御纵深）
- 极数必须为**正偶数**（交流绕组产生 2p 极主磁场）：3/5/7/9 极物理不存在，判 failed 并给出最近合法极数。
- 独立于 `parallel_circuits` 分支判定 —— 否则未填并联支路的行会整段跳过极数校验。
- 非法值（0 / 1 / 2.5 极）同样拦截。

### 📦 发版卫生修复（0.2.5 发布后核验发现，已随 0.2.5 一并生效）
`npm publish` 后解包 tarball 逐文件核验，发现三处本机绝对路径随包外发：

| 位置 | 内容 | 处置 |
|---|---|---|
| `scripts/loss_calib_v2_result.json` | `D:/MotorDesign/output/...` | 文件删除 + `.gitignore` 登记 |
| `scripts/publish.mjs` / `release.mjs` | 硬编码 managed Python 绝对路径 | 改读`MANAGED_PYTHON` 环境变量 |
| `scripts/_publish_0_2_{1,2,3}.py` | 旧版本发版脚本，含 `D:\` 绝对路径 | 用完即删（无任何引用） |

**根因**：`.gitignore` **管不住 npm 打包** —— `package.json.files` 是白名单，优先级高于
`.npmignore` / `.gitignore`。已在 `.npmignore` 顶部写明这一优先级，避免后来人误以为ignore 生效。

新增门禁 `P0 发布物无本机绝对路径泄漏`：按 `files` 白名单扫描全部发布目录，
匹配盘符绝对路径即失败。已用注入样本反向验证（注入 →❌，移除 → ✅），非"永远绿"断言。
门禁 **93 → 94/94**。

### 门禁
- `verify.mjs` **82 → 94/94**（新增 12 项：V16频率与断崖、V18频率合法性、绝缘透传三路一致、
  转矩口径、极数奇偶、V10/V16 频率口径不串扰、社区反馈闭环 4 项、PMSM 高速回归 4 项、
  发布物路径泄漏 1 项）。
- 订正 1 条固化缺陷的旧断言：原「1500rpm 应推荐 4 极」正是断崖根因本身，已按 `n_sync > n` 重写。
- 回归门 B **OK**（无劣化）；门 A 仍为既有 `small55kw` thermal DEBT，未新增欠账。
- 单测 22/22 通过。

### 📦 同批并入 0.2.5（2026-10-07 批次）：高速 PMSM 卡死修复 —— P5 能力门 + P0 轭厚按磁密反解 + P1 匝数量化语义拆分 + P2 磁密口径自洽

本版针对实测缺陷「200kW/22000rpm/380V/PMSM ⇒ 120/120 候选全 failed、`recommended=null`」做根因修复。
**修复前 0 可行 → 修复后 44 行通过校验、43 行可行**，且 `recommended=null` 不再是死胡同（能力门给出明确出路）。

#### 🚪 P5 新增：L0 能力门（适用域前置判定）
- 新增 `lib/applicability-gate.mjs`：在**调用前**按电频率 / 电压 / 转速 / 机种四维度判定 L0 是否适用，三档 `ok` / `caution` / `reject`。
- 标定域（基于实测划定）：`f ≤ 400Hz`、`n ≤ 3000rpm`、`U ≤ 690V`；PMSM 无 dq/弱磁/退磁模型，恒为 `caution`。
- `motor_l0_estimate` 返回体新增 `applicability` / `applicability_reasons` / `applicability_advice` / `applicability_note`。
- **核心价值**：`feasible_count=0` 时明确归因为「超出 L0 标定域」而非「设计上无解」，并给出可执行出路（升压 / 降速 / 增极 / 直接进 L1·L2）。
- 设计原则：能力门只判**工具适用性**，不判**设计可行性** —— 二者混淆会导致「工具越界 ⇒ 谎称设计不可行」。

#### 🔧 P0 轭厚按磁密需求反解（主修复）
- **根因**：`yokeThickness()` 恒等于 `(OD−Dsi)/2 × 0.40`，**无任何磁密约束**。高速大 Dsi 下只给 12.7~17.6mm，而 `By ≤ 1.9T` 需 21.9~48.4mm（缺口 7.7~30.8mm）⇒ V08 必然判饱和。**V08 判据本身物理正确，缺的是生成端配厚。**
- 新增 `yokeThicknessForFlux()`（`yoke_required = Bg·Dsi/(p·By_target·k_stack)`）与 `solveYokeAndFrame()`（取比例值与磁密需求值的较大者，轭厚放大时同步外扩 OD）。
- 实测收益（count=120）：`V08 failed ×120 → 0`；15kW 基线 `failed 80 → 40`；200kW/690V 不变。
- **硬约束优先**：`stator_od_limit` 是约束、磁密需求是目标，**目标不得覆盖约束**。上限内放不下足够轭厚时，如实回吐 `yoke_limited_by_od_cap` 并由 V08 判失败，不靠偷扩上限掩盖。
- `defaultOdLimit()` 同步 P0 感知（把轭厚增量计入上限），否则「默认配置」会悄悄抵消 P0 的修复。

#### 🔧 P1 匝数量化语义拆分（V14 修正 + 新增 V17）
- **根因**：V14 拿**已被 `Math.max(1,·)` 钳整**的匝数反算 E，再要求偏差 ≤10% —— 等于「用自己量化过的值校验自己的量化误差」，属逻辑循环。理想匝 0.716 → 钳成 1 匝 ⇒ E 306.6V vs 219.4V（偏差 39.8%）**必然自证失败**。
- **职责拆分**：
  - `V14`（改名「反电势自洽性」）= 只抓「填错」。仅当理想匝 ≥1（无钳位放大）且行内匝偏离理想值 >2× / <0.5× 时判 failed。
  - `V17`（新增「匝数整量化可行性」）= 判物理可实现性。理想匝 <1 ⇒ 圆线整匝绕组无解，报错**必带可执行出路**（升压倍数 / 降频增极 / 加大 Dsi·L / 分数槽）。
- `deriveElectricalClosure` 新增 `turns_per_coil_ideal` / `turns_quant_dev` / `turns_rounded_up`，量化偏差显式可查。
- **避免重复归因**：理想匝 <1 时不再报 V14（否则会把「整匝无解」误导成「参数填错」，诱导用户去改本来正确的电压/几何）。

#### 🔧 P2 气隙磁密按工况自洽
- **根因**：`AIR_GAP_FLUX_DEFAULT=0.80` 全程硬编码，且 By 公式不含转速 ⇒ 同几何下 `n=250` 与 `n=22000` 的 By **逐行相同**，V08 结构上无法区分高速。
- 新增 `airGapFluxActual()`：由**实际整数匝数**反解气隙磁密（`Bg_actual = Bg_intent · N_ideal/N_int`），V07/V08 改用它判定 —— 铁芯真正承受的磁密由实际匝数决定。
- **消除自相矛盾**：此前 V14 说「E 偏差 39.8%」（按实际匝数）、V08 却按意图匝数说「By 正常」，同一物理量两个口径。
- 可回退：`bg_caliber='intent'` 回到 v0.2.4 旧口径，便于对外口径对齐与历史对比；`metrics.flux.bg_caliber` 回吐所用口径。

#### 📉 撤回一项此前的错误结论：并联支路方向（P3）
- 此前分析称「`turns = N·a·3/Qs` 把并联支路 a 乘进去、方向反了，应改成除 a」。**经数值验证该结论错误**：
  `a` 条并联支路并联在同一相电压上 ⇒ 每支路串联匝数必须都是 N ⇒ 每线圈匝 = N·3a/Qs **确实应乘 a**。
  实测：`a=1→8` 时每相串联匝恒定 111.33、E 恒定 219.4V；若错误地改成除 a，E 将崩至 2.9V（偏差 98.7%）。
  ⇒ **代码保持原样**，并加单测固化该不变量，防止后续误改。

#### ✅ 门禁与文档
- `verify.mjs` **70 → 最终 94/94**（本批 P0/P1/P2/P5 共 9 项，压测复核 6 项，社区反馈闭环 4 项，
  PMSM 高速回归 4 项，发布物泄漏 1 项 —— 逐批累计；单批数字见各段）。单测 22/22。
- 校验规则共 **17 条**（V01–V12、V14–V18；V13 已并入 V06）。
- 回归门 B 无劣化；门 A 仍为 DEBT（既有 `small55kw` 越带 1.75pt，未受本版影响）。
- 修正两条**编码了旧缺陷行为**的断言：①「2极轭饱和必然全批 failed」（其前提「无论轭厚如何都无法避免饱和」经 P0 证伪）；②「默认上限与旧写死 450 逐字节一致」（P0 使部分行 OD 合法增长至 490mm，前提不再成立，改为断言「未放大行逐字节一致 + 默认上限 P0 感知且被严格尊重」）。
- 校验规则文档此前停留在 12 条且把 V08 记为 `warning`，已按 `RULE_CATALOG` 校正（V08 自 v0.1.7 起即硬门禁 `failed`）；本版最终为 **17 条**。

## [0.2.4] - 2026-10-07 — 代理模型 v2.3.0-fixseg 部署 + GBR 残差校正启用

### 🚀 代理模型升级：v2.2.0 → v2.3.0-fixseg
- **特征空间扩展**：5 特征（含 `poles`），OD 三分位切段（small/medium/large × induction/pmsm）。
- **模型锁定**：云端重训流水线锁定 `scikit-learn==1.9.1`（`numpy==2.5.0` / `scipy==1.18.1`），复现 G7 门禁（纯 numpy 回放 vs sklearn 重训 maxdiff < 1e-6）。
- **校验**：`verify.mjs` 70/70 通过；代理模型版本断言 `2.3.0-fixseg`。

### ✨ 新能力：GBR 残差校正（ridge_plus_gbr 第二阶段）
- `lib/surrogate-engine.mjs` 新增 `predictGBR()`，消费模型每段 `gbr` 字段（400 树 / `lr=0.05`），对 ridge 基线做残差修正：`final = primary + gbr_residual`。
- 单测新增 4 例（无 gbr 退化为 0、残差量级合理、预测携带 `used_gbr` / `gbr_residual_pp`），单测 18 → 22 全绿。
- 置信度门控维持 ridge 通道口径（不放大降级），仅提升预测精度。

### 🔧 工程
- `tools/l0/pipeline.mjs` 聚合工具随包发布（一步跑完 `buildParamMatrix → runDesignValidate → runL0Estimate`，规避 DSH 2000 行截断）。
- 版本三重一致性：package.json / SKILL.md metadata / CHANGELOG 同步至 0.2.4。

### ⚠️ 关于 V16
- 0.2.4 发布时曾记录「V16 未并入本版本」。**该记录与实际发布物不符**：`npm publish` 从工作树打包，
  已发布的 0.2.4 包内 `design-rules.mjs` **实际含 V16**（解包核验：V16 × 5 处）。
  0.2.4 的 git 提交随后已通过 amend 补齐，使 git 与 npm 内容一致。

## [0.2.3] - 2026-09-27 — 默认通道回归修复 + 对外物料（推广就绪）

### 🔴 P0 回归修复：`auto` 不再进入代理通道
- **现象**：0.2.2 把 `l0Mode='auto'` 纳入代理分支后，异步机默认输出严重残缺 —— `temp_rise=0`、`tooth_flux_density`/`yoke_flux_density`/`power_factor` 全 `undefined`、`feasible_count=0`（120 个候选无一可行），且 `sort_by='temp_rise'/'total_loss'/'torque_density'` 全部退化为 0，排序失效。
- **根因**：`buildSurrogateResult()` 只输出 `efficiency` 一项，温升 / 损耗 / 磁密 / 可行判定均为 0 或缺失。代理通道本就只适合"效率排序"，不适合做默认。
- **修复**：`tools/l0/l0-estimate.mjs:72` 代理分支条件回退为仅 `l0Mode === 'surrogate'`；`auto` 走字段完整的公式通道。代理模型 v2.2.0 与显式 `surrogate` 通道保持可用。
- **验证**：`verify.mjs` 61/61 通过；异步 15kW/1460rpm `auto` → `feasible_count=80`、Top1 效率 95.17% / 温升 14.6K / 轭磁密 1.39T（字段完整）。

### 📣 对外物料（推广就绪）
- **README**：删除 0.1.7 时代过时的「surrogate 实验特性 / cv_r2≈0 / 特征泄漏」警告，替换为 v0.2.2 真实指标表（异步三段 OD 覆盖 / 样本数 / CV R² / MAE，PMSM 自动降级）；新增价值主张、示例输出（15kW 实跑 16 ms Top5）、通道分工表、明确不适用工况表；配置表补 `surrogatePath` / `surrogateConfidenceThreshold`。
- **examples/**（新增，随包发布）：3 个真实算例 —— ① 15kW/1460rpm 异步（80 可行，16 ms）② 75kW/1480rpm/660V 异步（效率封顶并列时的二次排序）③ **200kW/22000rpm 高速 PMSM 反例**（`recommended=null`，公开能力边界）。
- **keywords**：9 → 20（补 `induction-motor` `motor-sizing` `ansys` `电机设计` `永磁同步电机` `异步电机` 等中英文关键词）。
- **assets/marketplace-listing.md**：0.1.0 → 0.2.2，重写 listing 文案、实测性能表、截图清单、npm 检查清单（含 npmjs 官方源踩坑提示）。

## [0.2.2] - 2026-09-26 — 代理通道修复（模型 v2.2.0）

### 🔴 P0 修复：代理模型 coef 空间错位（0.2.1 升级不可用根因）
- **现象**：用户开启 `l0Mode='surrogate'` + 新模型 `l0_surrogate_family.json` 时，异步机预测效率**恒为 50.00%**（物理钳位下界），`prediction_source=surrogate` 且 `surrogate_fallbacks=0` 误报「无降级」。
- **根因**：训练端 `fit_segment` 在**原始特征空间**解 Ridge（coef 截距≈19、权重极小），而推理端 `extractFeatures` 先把特征按 `feature_mean/std` 标准化再乘 coef → 空间错位，`rawEff≈19` 被钳到 50。CV 评估在标准化空间做的（R² 高），但导出 coef 来自原始空间训练，两端不一致。
- **修复**：`fit_segment` 改为在「按全量原始均值/标准差标准化」的特征空间内训练（feature_mean/std 维持原始统计不动），训练/推理空间严格对齐。重训导出模型 **v2.2.0**。
- **验证**（端到端）：异步 75kW/1480rpm → `eff≈89~91%` 且 `source=surrogate`；PMSM 200kW/22000rpm 因段 cv_r2≤0.31、conf<0.4 仍正确降级公式。

### 默认配置同步（让代理通道真正可用）
- `index.mjs` Config schema 默认值：`surrogatePath` `'models/l0_surrogate.json'`→`'models/l0_surrogate_family.json'`；`surrogateConfidenceThreshold` `0.7`→`0.4`。
- `tools/l0/l0-estimate.mjs`：代理分支条件由 `l0Mode==='surrogate'` 扩展为 `'surrogate' || 'auto'`（插件默认 `l0Mode='auto'`，否则代理通道永不启用）。
- `cordis.patch.yml`：同步 `surrogatePath` / `surrogateConfidenceThreshold`，`l0Mode` 保持 `auto`。

### 测试
- `scripts/verify.mjs` 新增「surrogate 通道端到端可用且不被钳底」用例（显式走代理 + 断言 `efficiency>50 && ≤99`）。全量 **61/61** 通过（原 60/60 + 1）。

## [0.2.1] - 2026-09-26 — 干净集重训（v2.1.0）

### 数据清洗
- **排除 fault=1 失败样本**：`l0_residuals` 中 992 条异步 + 30 条永磁标记为 RMxprt 求解失败（l1_eff=0），全部剔除。干净集 n=3835（异步）+1100（永磁）。
- **根因分析**：fault=1 样本集中在大极数(6p)+大 OD 设计，推测 RMxprt 在该参数空间发散，应在 L0 阶段直接标记不可用而非进入代理模型。

### 性能对比（v2.0.0 → v2.1.0）

| 段 | n | CV R² | CV MAE | l0 基线 MAE | 相对改善 |
|---|---|---|---|---|---|
| 异步 small | 1279 | **+0.652** | 1.44pp | 3.68pp | **−61%** |
| 异步 medium | 2558 | +0.438 | 3.29pp | 5.31pp | −38% |
| 异步 large | 1276 | **+0.670** | 4.53pp | 8.69pp | **−48%** |
| 永磁 small | 367 | +0.134 | 2.17pp | 2.09pp | — 公式降级 |
| 永磁 medium | 745 | +0.099 | 1.88pp | 1.83pp | — 公式降级 |
| 永磁 large | 355 | +0.311 | 1.29pp | 1.68pp | −23% |

### 扩充决策（否决）
- `case_motors` 表 `wire_dia` 字段 **100% NULL**，无法补算 l0_eff（公式通道需导线直径计算铜耗）。
- 铭牌效率（平均 82.4%）与 RMxprt 效率（平均 89.5%）口径不一致，强行并入会注入错误残差关系。
- **结论**：扩充暂缓，待 wire_dia 补全或铭牌→RMxprt 映射标定后再做。

## [0.2.0] - 2026-09-26

### 修复
- **`estimateTotalLoss()` 量纲修复**（`lib/surrogate-engine.mjs`）：`efficiency` 是百分数口径（91.34=91.34%），旧实现 `powerW*(1/efficiency-1)` 按小数口径计算，15kW/η91.34% 会得到 **−14835.78W**（负损耗）；现先归一到 [0,1] 再反推，同输入得 +1422.17W，并增加 η≤0 或 η≥100% 的防护（返回 0 交公式通道兜底）
- **`V06` 补充 `a ≤ q = Qs/(3p)` 判据**（`lib/design-rules.mjs`）：旧实现只校验 `a|p`，会放行 `p=4/Qs=36/a=4`（q=3）这类支路无法分配的不可实现设计；现整数槽下 `a>q` 判 `failed`，分数槽（q 非整数）不在此判据范围。规则注册表/`ENGINEERING.md`/skill 参考文档同步更新

### 变更
- **surrogate 通道标注实验特性**：`models/l0_surrogate_family.json` 增加 `experimental: true` 与 `experimental_note`（训练域 od_range≈612~2651mm 与真实中小机座 100~500mm 不匹配、PMSM 段 cv_r2≈0、特征含 l0_eff 泄漏）；`motor_l0_estimate` 工具输出新增 `surrogate_experimental` / `surrogate_warning` 字段；README 增加 ⚠️ 标注。排序决策请使用默认公式通道
- **仓库卫生**：`git rm --cached` 移除历史误入库的 `dist/`（4 文件，含 _debug*.py 与 v0.1.0 tar.gz）与 `metadata/publish-log.json`（本地文件保留，`.gitignore` 规则自此生效）

### 新增（代理模型 v2.0.0 重训 · 基于 6390 行真实 RMxprt 残差）

- **数据源修正**：旧 v1.0.0 误用 242 行 `res_cc` 真解且为"去泄漏"删掉 `l0_eff` → 小/中机 cv_r2 为负。v2.0.0 改用 `designs.db.l0_residuals`（6390 行：5260 异步 + 1130 永磁，含 `l0_eff` 公式效率 + `l1_eff` RMxprt 效率）为主训练源（决策"先用现有数据训练，不调用 RMxprt"）。
- **特征工程（决策"加入特征"）**：特征集 = `[stator_od, stator_id, core_length, poles, l0_eff]` → `l1_eff`。实证 `l0_eff` 是公式通道可算的物理基（推理端公式通道会算，不依赖 RMxprt），非部署泄漏；加入后异步 cv_r2 由 +0.21（纯几何）跃升至 +0.55。异步分族：small R²=0.636/MAE=1.45pp、medium 0.484/3.57pp、large 0.670/4.81pp。
- **扩充消融**：`case_motors`（铭牌效率）按 0.5× 并入经消融验证会拉低异步 R²（0.547→0.537，标签口径铭牌 vs RMxprt 不一致），故 v2.0.0 暂未并入，待 nameplate→RMxprt 映射标定后再扩充（决策"扩充"暂缓）。
- **PMSM 处理**：各段 cv_r2≤0.31（效率集中高位、方差小）→ 不建族，推理自动降级公式通道。

### 修复（插件部署打通）

- **版本校验**（`lib/surrogate-engine.mjs`）：`startsWith('1.')` 拒 v2.0.0 → 改为接受 `'1.' | '2.'`。
- **detectMotorType**：`od<400→pmsm` 误判真实小异步（OD 275/337）→ 改为优先用 `params.motor_type`，回退 OD 启发式。
- **置信阈值**：`DEFAULT_CONFIDENCE_THRESHOLD` 0.7 高于本模型最高 R²(0.67) 会导致代理永不启用 → 降到 0.4。
- **l0_eff 注入**（`tools/l0/l0-estimate.mjs`）：`predictSurrogate` 读取 `params[l0_eff]` 但推理 `params` 无此字段 → 推理端用 `quickL0Estimate` 补算注入（实测 Python/Node 两套公式仅差 ~0.5pp，口径一致）；默认模型路径改为 `models/l0_surrogate_family.json`。

### 校验
- 模型 JSON 结构与 `predictLinear`/`extractFeatures` 对齐（`coef`=[截距+5权重]，`feature_mean/std` 齐备）。
- 推理端自校验（Task #39）：注入 l0_eff 后 `predictSurrogate` 不再抛"特征 l0_eff 缺失"，异步各段返回合理效率区间。
- `scripts/verify.mjs`：**60/60 全绿**（新增 V06 `a≤q` 三态断言：a=4 拦截 / a=2 放行 / a=3 整除判据回归保护）

## [0.1.7] - 2026-09-23

### 修复（L0 薄轭模型失真 · V08 轭磁密门禁硬化）

审计《重跑结果 #1/#5》发现 L0 薄轭模型 `By` 估算虽公式正确（与齿磁密 `Bt` 同源 `By = B·Dsi/(p·yoke·k)`），但 `V08` 仅作 `warning`，未剔除轭饱和候选，导致 `Bj∈[2.88,5.29]T` 全部超过硅钢饱和极限（≈2.0~2.1T）却仍判 `feasible=true`，下游误收到「超标轭磁密方案」。本版将轭饱和判据硬化为**阻断级门禁**：

- **新增轭饱和物理常量**（`lib/motor-constants.mjs`）：`B_YOKE_WARN_T=1.5`（预警）、`B_YOKE_SAT_T=1.9`（硅钢饱和极限，工程工作上限）
- **`V08` 规则体硬化**（`lib/design-rules.mjs`）：`By > 1.9T` 直接 `failed`（轭部无法承载磁通，几何不可行）；`1.5T<By≤1.9T` 仍 `warning`；`|By−0.82T|` 偏差按 `FLUX_TOLERANCE` 分层 warning
- **`quickL0Estimate()` 可行性聚合**（`lib/formula-engine.mjs`）：新增 `yokeSat = yokeB > B_YOKE_SAT_T`，`feasible = backEmfOk && slotFillOk && thermalOk && !yokeSat`；native/result 新增可追溯字段 `yoke_sat`（已纳入 `L0_NATIVE_FIELDS`）
- **物理结论（重要）**：2 极 200kW/22000rpm 种子几何下 `Dsi/p` 比导致轭磁密**物理下界 ≥2.1T**（`yoke→(OD−ID)/2` 极限仍超 1.9T），即 0.82T 轭磁密目标在该尺寸/极数下**不可达**——须放大定子外径或改用 4 极才能落地 0.82T 目标。V08 硬化后 `feasible_count=0` 是**正确物理结论**，而非求解器失真

### 校验
- `scripts/verify.mjs`：**59/59 全绿**（新增 V08 轭饱和 `failed` 断言 + 轭饱和阻断闭环断言；修正 `yoke_sat` 字段作用域 bug）
- 注：原「液冷 200kW feasible_count>0」断言已改为「轭饱和 ⇒ 全部 infeasible 且不推荐超标方案（V08 硬化）」，如实反映物理不可达

## [0.1.6] - 2026-09-23

### 修复（P0 · L0 电磁链物理自洽）

审计《DSH_L0水冷方案审计报告》发现 4 项致命缺陷，本版在 L0 层补齐**电气闭环反推**，使重跑结果物理自洽、可直接交接 L1 精算：

- **电气闭环反推（核心）**：新增 `deriveElectricalClosure()`，按真源物理式反推
  - 反电势 `E ≈ 4.44·f·N·Φ·kdp`、匝数 `N = Uph/(4.44·f·Φ·kdp)`、电流 `I = P/(√3·U·pf·η)`、星接 `Uph = U/√3`、峰值→有效 `I_rms = I_pk/√2`
  - 每极磁通 `Φ = 2·Bg·Dsi·L/p`（与 `design-rules.mjs` 同源 2/π 平均因子）
  - 200kW/22000rpm/380V/PMSM 实测：峰值电流 496.9A、反电势 219.4V≈相电压、频率 366.7Hz，与审计反算吻合
- **铜损/槽满率重写**：`estimateCopperLoss()` 改用**真实电流**（peakCurrent/turnsPerCoil/parallelCircuits）算相电阻、铜损、槽满率 `sf`，输出 `feasible`（≤`SLOT_FILL_MAX`）
- **估算输出接入闭环**：`quickL0Estimate()` 用 `effTurns/effCurrent` 驱动损耗链，新增 18 个可追溯诊断字段（`electrical_frequency_hz`/`back_emf_v`/`slot_fill_ratio`/`peak_current`/`turns_per_coil`/`feasible`/`verdict` 等）
- **新增两条 P0 门禁规则**：
  - **V14 反电势闭环**（failed）：用行内 `turns_per_coil` 反算 `E`，`|E−Uph|/Uph > 10%` 判不自洽（抓「匝数与电压/频率失配」）
  - **V15 槽满率可行性**（failed）：`sf > 0.78` 判几何不可实现（须增大机座或降电流）
- **PMSM 去异步污染**：`motor_param_matrix` 对 PMSM 强制 `slots_rotor=0`（无笼型槽）、定子槽取 `PMSM_SLOT_MAP`；`rotor_type='pm_synchronous_no_cage'`
- **换热系数标定修正**：`COOLING_COEFFICIENT.liquid_jacket` 350→700（油冷/浸油相应上提），液冷 200kW 温升 76K≤F级105K 可行
- **匝数下限放宽**：V13 改为 `<1` 才 fail —— 380V/高频闭环可能给出 N=1（hairpin 扁线绕组可行），不再误报

### 校验
- `scripts/verify.mjs`：**58/58 全绿**（新增 6 条闭环/门禁断言：电流≈497A、PMSM 无转子槽、反电势≈Uph、液冷 feasible、V15 槽满率、V14 反电势）
- `scripts/regression.mjs`：门 B 回归 **OK（无劣化）**；门 A 仍判 **DEBT（非阻断）**——small55kw 1.75pt 为 L0 未标定参考带偏差，与 0.1.5 同口径；baseline 已重快照锁定修正后物理行为
- 闭环矩阵 `validateDesignBatch`：0 failed / 20 warning（warning 多为 V08 轭磁密偏高的 L0 薄轭模型局限，留待 L1 精算）

### 说明
- 本版本聚焦 **L0 电磁链治本（P0 四条致命缺陷全修）**，未触碰脱敏回传（0.1.5）与 PMSM 口径（0.1.4）逻辑，零回归。

## [0.1.5] - 2026-09-22

### 新增
- **脱敏聚合指标回传（显式可选，默认关闭）**：新增 `lib/telemetry.mjs`（零新增依赖，
  仅用 `node:fs`/`node:os`/全局 `fetch`），在 0.1.4 的本地 `usage.jsonl` 之上补一条
  **可选的**脱敏上报通道，配套管理端「DSH 插件管理 · 插件使用情况」卡片闭环：
  - `sanitizeTelemetry()`：字段白名单 `TELEM_FIELDS`（tool/ok/elapsed_ms/n/success/
    failed/warning/sort_by/top_n/error/ts）单一真源硬编码；逐条记录裁剪到只剩白名单，
    设计参数正文、结果明细（效率/温升/损耗）、提示词、工作目录、密钥、client_id 一律丢弃。
  - `aggregateUsage()`：把脱敏记录**按工具聚合成统计量**（调用次数/成败/耗时分布/规模总和/
    成功速率/来源版本），只上聚合值、不上逐条明细；平台元数据仅保留 OS + node 大版本。
  - `reportTelemetry()`：分批（`telemetryBatchSize`）POST 到 `telemetryEndpoint`，
    按字节 offset 增量推进，**HTTP 失败/超时不推进 offset（下批重报）且静默降级**，
    绝不影响工具主流程；`attachSessionTelemetry()` 运行时探测 DSH Runtime 的
    `sessionTelemetry` 瀑布并附带（不静态 `inject`，规避 apiProxy 永久 pending 事故）。
- **Config 新增 5 个字段**（`index.mjs`）：`telemetryEnabled`（默认 `false`）、
  `telemetryEndpoint`（默认 `''`）、`telemetryBatchSize`（默认 50）、
  `telemetryIntervalSec`（默认 300，0=仅退出 flush）、`sessionTelemetry`（默认 `auto`）。
  **须 `telemetryEnabled=true` 且 `telemetryEndpoint` 非空才真正启用**（显式 opt-in）。
- **`apply()` 生命周期**：`setupTelemetry()` 周期 flush + 进程退出（SIGTERM/SIGINT/
  beforeExit）flush 一次；启动日志追加「脱敏回传 on/off → 端点」状态。

### 合规底线
- 默认全关：不配置 = 零外发，`usage.jsonl` 照本地记录，与 0.1.4 行为逐字节一致（零回归）。
- 只回传白名单统计量；`error` 字段截断到 80 字并去换行/路径，避免夹带参数/密钥。
- 逐条明细只在本机（`usage.jsonl`），外发的是聚合 payload。

### 说明
- 本版本聚焦 **信息回传能力（v3 定案 option-2 + option-3）**，未改动 L0 求解/校验/
  口径逻辑，49 项回归基线不受影响（`node scripts/verify.mjs` 全绿）。


## [0.1.4] - 2026-09-22

### 新增
- **PMSM（永磁）id/od 生产口径**：`lib/motor-constants.mjs` 新增 `idRatioByPoles(poles,
  {isPm, legacy})` 与 `isPmsmType(motorType)`，复刻程序侧 `physics_kernel.py:547-562`
  的 `id_ratio_by_poles` 三分支真源——
  - 永磁 `is_pm`：`0.72 + 0.010(p−2)`，钳 `[0.70, 0.80]`（`_ID_OD_PM_MIN/MAX`）
  - 生产异步：`0.55 + 0.012(p−2)`，钳 `[0.45, 0.65]`（`_ID_OD_ASYNC_MIN/MAX`）
  - legacy（零回归基线）：`0.55 + 0.03(p−2)` 无钳，与 0.1.3 / 程序 focused_scan 一致
- **`motor_type` 入参（opt-in）**：`motor_param_matrix` 新增 `motor_type` 入参，`motor_design_validate`
  V03 期望比同口径联动。传 PMSM/BLDC/IPM（含中文别名，判定复刻 `PMSM_TYPES`）时，定子内外径比按
  永磁生产口径生成（更贴合 200kW/22000rpm 这类 PMSM 主场景真实几何，id/od 落 0.70~0.80）；
  不传则维持 legacy 口径，**默认路径逐字节零回归**（缺省不产生 `motor_type` 键）。
- **knowledge-sync 真源指针修正**：`sync-constants.py` 原用 `PLUGIN_ROOT.parents[0]` 相对推导，
  独立仓（`D:\dsh-motor-ai-l0`）下解析成盘符根 `D:\`（无 `Scripts/`）→ `--check` 全判
  `source_missing`（假阴性）。现显式指真源仓 `D:\MotorDesign`，支持 `MOTOR_AI_REPO_ROOT`
  环境变量覆盖其它机器。

### 说明
- 本版本聚焦 **P0（PMSM 口径对齐 + 真源指针修正）**。程序侧 v4.02.x 另有的
  `LAMBDA_BAND_BY_POLES`（λ 分档带）与 `_DEFAULT_TYPE_BOUNDS`（类型可解区间）
  两块属「全对齐」范畴，**未在本版引入**，留待后续版本，以保护既有 49 项回归基线。

## [0.1.3] - 2026-09-22

### 新增
- **本地用量统计（零依赖 JSONL）**：每次工具调用向
  `~/.dsh/storages/dsh-motor-ai-l0/usage.jsonl` 追加一行元数据
  （`ts` / `tool` / `ok` / `elapsed_ms` / `n` / `failed` 等）——只记次数、耗时、
  规模与成败，**不记设计参数与结果内容**（行业 Know-how 留在会话内）。
  新增配置项 `usageLog`（默认开）；新增 `lib/usage-log.mjs`（所有 IO 静默降级，
  绝不影响工具主流程）。数据可直接用脚本聚合，或作为社区统计面板
  （`dsh-usage-statistics-panel` / `dsh-usage-unified` 等）的本地数据源。
  > 刻意**不采用** `inject: ['telemetry']`：核心遥测服务实名是 `sessionTelemetry`
  > （`dsh-session-telemetry-otel`），不存在名为 `telemetry` 的可注入服务，
  > 照抄会令插件永久 pending（同 apiProxy 事故机理）。

### 变更
- **工具链自描述修复**：三个工具的 `description` 显式声明流水线顺序与输入契约——
  `motor_l0_estimate` 写死「`params_list` 必须由 `motor_param_matrix` 生成，
  不接受散装命名参数」（实测首调必败点），`motor_param_matrix` /
  `motor_design_validate` 双向标注上下游。
- **SKILL 触发精确化**：`SKILL.md` frontmatter 新增 `whenToUse`（英文触发场景，
  runtime `SkillEntry.whenToUse` 实锤支持）与 `user-invocable: true`
  （可 `/motor-l0-estimate` 手动触发；注意必须用 kebab-case，
  camelCase `userInvocable` 会被 loader 当 legacy 键拒绝）；`description`
  补中文触发词（电机设计/估算/选型/参数矩阵/方案初筛）。

## [0.1.2] - 2026-09-22

### 变更
- **定位升级**：由「L0 快速预筛层」升级为「电机AI辅助设计软件插件」，覆盖电机设计全流程
  （需求解析 → 参数识别 → 候选矩阵 → L0/L1/L2 解算 → Top10 与校验 → 完整方案）；
  L1 RMxprt / L2 Motor-CAD 明确标注为**可选**精算工具。
- `package.json` 的 `description` 与 `keywords` 同步更新
  （keywords 新增 `dsh` / `motor-ai` / `l0-estimate` / `rmxprt` / `motor-cad`）。

### 文档
- **README 重构（对外）**：新增「快速开始」（可复制的 npx 安装命令 + 三工具标准流水线 + 踩坑提示）、
  「配置」表（11 项逐条说明）、「已知精度边界」；内部工程内容移出，正文精简至对外可读规模。
  - 补充关键提示：`motor_l0_estimate` 只接受 `params_list`，**不接受** `power_kw` / `speed_rpm`
    等散装命名参数，须先用 `motor_param_matrix` 生成矩阵。
  - 修正安装命令：`dsh` 未进 PATH 时须用 `npx -y @deepseek-ai/dsh plugin ...`。
- **新增 `docs/ENGINEERING.md`**（工程内参）：目录结构（同步至真实文件清单，含
  `surrogate-engine.mjs` / `models/` / `tests/` / `knowledge-sync/` / `manifest.json` / `assets/`）、
  字段契约 Param 锁、12 条校验规则、与 Python 体系 3 处差异、6 项资料稿偏差修正、W4 复盘、路线图。
- **新增 `docs/QUALITY-GATE.md`**（质量门）：两道门 + divergence 诊断、5 项偏差根因、
  首轮实算对照、待校准清单与翻转判据。
- **新增 `LICENSE`（MIT）**：`package.json` 早已声明 `"license": "MIT"` 但仓库缺少许可文件，
  本次补齐并加入 `files`。

### 修复
- **字段笔误**：`L1_MIRROR_FIELDS` 实为 **7** 项（旧文档写 8 项）；
  `l1_efficiency_proxy` 属诊断列 `L1_DIAG_FIELDS`，不是镜像字段。
- **路线图状态修正**：W5 由「待开始」更正为「完成」（代理模型 / SKILL.md / release.mjs 均已交付）；
  W6 由「待开始」更正为「部分完成」（npm 已发布）。
- `index.mjs` 头部过期注释（「本阶段不注册任何 tool」）更正为「注册 3 个 L0 工具」；
  `tempRiseRange` 语义描述澄清为「`max_temp` 钳位区间（°C），非 L0 温升（K）」。

## [0.1.1] - 2026-09-21

### 修复
- **工具参数 schema 运行时加载失败**（`dsh web` 启动报 `unsupported JSON schema: parameters.voltage_v.required must be true when present`）：
  - DSH `dsh-tools` 的 schema 编译器规定 `required` 一旦出现，值必须为 `true`；可选参数不得写 `required: false`。
  - 删除 `tools/l0/param-matrix.mjs`、`l0-estimate.mjs`、`design-validate.mjs` 三个工具全部可选参数的 `required: false`（保留真正必填项的 `required: true`）。
  - `design-validate.mjs` 的 `params`（`type: 'object'`）补 `additionalProperties: true`（编译器对 `object` 类型强制要求显式 `additionalProperties`）。
  - 已用真实 `@deepseek-ai/dsh-tools` 的 `defineTool` 对三个工具的 `TOOL_PARAMS` 做编译校验，全部通过：`motor_param_matrix` required=[power_kw,speed_rpm]、`motor_l0_estimate` required=[params_list]、`motor_design_validate` required=[]。

- **插件 apply 退出时报 `Invalid effect`**（`dsh web` 启动报 `TypeError: Invalid effect`，来自 cordis `safeCollect`）：
  - cordis 把 async 插件 `apply()` 的返回值当作「effect（清理句柄）」收集；返回普通对象 `{ isLevelEnabled, tier }` 既非函数也非 null/undefined，触发 `safeCollect` 抛错。
  - 删除 `index.mjs` `apply()` 末尾的 `return { isLevelEnabled, tier }` 块，使 apply 返回 `undefined`（无 effect），cordis 正常放行。
  - 该返回对象无外部消费方（`tierOf` 仅在日志行内联使用，`verify.mjs` 直接 import `lib/level-gate.mjs`），删除无副作用。

## [0.1.0] - 2026-09-21

阶段目标：W1-W4 — L0 预筛层可交付核心（依据《MotorAI_L0第一阶段实施方案_v3.docx》）。

> 说明：W2-W4 在同一版本号下增量交付，未单独发版；条目按阶段分组记录。

### 新增
- 插件骨架与生命周期：
  - `package.json`：DSH Bundle 声明，`files` 含 `cordis.patch.yml`，无 `mcpServers`、无 Python 依赖
  - `cordis.patch.yml`：配置层补丁，含层级总闸 `level: 'l0'` 与 L1/L2 预留注释
  - `index.mjs`：`Config` Schema + `apply()` 生命周期，启动即对越级配置快速失败
- `lib/level-gate.mjs`：层级门控
  - `assertLevelImplemented()`：非 `l0` 立即抛错，杜绝静默降级
  - `isLevelEnabled()`：总闸 + 分闸双层判断
  - `tierOf()`：层级 → 付费等级映射（l0=free / l1=vip / l2=flagship）
  - 零第三方依赖，可被 Node 直接 import 做离线断言
- `scripts/verify.mjs`：离线校验
  - `files` 必须含 `cordis.patch.yml`（否则安装方拿不到配置层）
  - `cordis.patch.yml` 结构校验（insert / id / level 默认 `l0`，L1 分闸不得默认开）
  - `CHANGELOG.md` 含当前版本条目；`SKILL.md` 三重一致性（W5 交付后生效）
  - 全量 `.mjs` 通过 `node --check`
  - level-gate 行为断言共 7 组

### 约束
- L0 为纯 JS 零依赖层：不调用 RMxprt、Motor-CAD、Python 桥接；相关能力归入 L1/L2

### 校验
- `node scripts/verify.mjs` 应全部通过（W4 后为 49/49）
- 越级配置（`level: 'l1'`）必须触发 `assertLevelImplemented` 报错而非静默降级

---

### W2-W3 增量（同版本）

- `lib/param-schema.mjs`：Param 锁 —— 字段单一真源，钉死在 `physics_kernel.py:618-646`
  与 `motor_tools.py:726-778`；导出 5 个互斥字段集合 + `pickL1Payload()` / `buildHandoff()`
- `lib/motor-constants.mjs`：常量层，全部标注 Python 真源行号；P3 高速扩表（10/12 极）
- `lib/formula-engine.mjs`：物理损耗通道（铜损 τ_p 端部物理式 / Steinmetz 铁损 / 机械损）
  + L1 同口径诊断列 `l1_efficiency_proxy` / `l1_temp_proxy`
- `tools/l0/param-matrix.mjs`：`motor_param_matrix`，复刻 `focused_scan` 且去随机化
- `tools/l0/l0-estimate.mjs`：`motor_l0_estimate`，双字段结果 + TopN 排序 + L0→L1 交接载荷
- 修订资料稿偏差 6 项（详见 README 第五节），含冷却系数量级重标定与「1 极档」物理错误作废

---

### W4 增量（同版本）

- `lib/design-rules.mjs` + `tools/l0/design-validate.mjs`：`motor_design_validate`
  - 12 条物理一致性规则（V01~V12），三级结论 `passed / warning / failed`
  - `escalate` 可把软规则临时升为硬剔除；缺输入记入 `skipped` 而非静默通过
  - 批量模式产出 `rule_hits`，可直接定位参数矩阵的生成偏差
- `lib/regression-gate.mjs` + `scripts/regression.mjs` + `benchmarks/`：回归质量门
  - 门 A 物理边界（能效带 + 绝缘限值）、门 B 回归（基线快照劣化检出）、诊断 divergence
  - `SIM_VALID_DOMAIN` 外案例标记 `out_of_domain`，不计入 2pt 预算
  - `--update-baseline` / `--strict` / `--json` 三种运行模式
- `index.mjs` 启用工具注册（3 个工具）；新增 `airGapFluxT` / `insulationClass` / `highSpeedRpm` 配置

### W4 修复
- 磁密公式补正弦平均因子 2/π（`Bt = 2·B_gap·Dsi/(Qs·bt·k)`，修正后 1.04T 对齐现场 1.02T）
- V12 温升默认降为 warning（未标定模型不应批死整批，实测 40/40 → 0/40）
- 槽宽深比下界 0.30 → 0.20（26/40 误报）
- 新增高速气隙档 `highSpeedRpm=8000` / `AIR_GAP_MAX_HIGH_SPEED=4.0`

### W4 已知欠账
- 质量门判定 **DEBT**：6/6 案例效率低于参考带 0.35~2.96pt，温升普遍越考核限值
- 根因同 README 第九节 5 项（损耗模型未标定 + 散热筋未计），需 RMxprt 批量结果回归后收紧
- `gate.sanity_blocking` 标定收敛后必须翻为 `true`

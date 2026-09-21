# npm publish 检查清单

## 前置条件

### 1. npm 账号与 Token
```bash
# 检查当前登录状态
npm whoami

# 若未登录，执行（需 npm token）
npm adduser --registry https://registry.npmjs.org
# 或使用 token：
npm publish --registry https://registry.npmjs.org //registry.npmjs.org/:_authToken=NPM_TOKEN
```

**当前状态**：⚠️ 未登录（`npm whoami` 返回 ENEEDAUTH）

### 2. 包名冲突检查
```bash
npm view dsh-motor-ai-l0
# 当前返回 404 → 包名未被占用 ✓
```

### 3. 私有 registry 选项
| 选项 | registry URL | 适用场景 | 成本 |
|---|---|---|---|
| npm 公开 | https://registry.npmjs.org | 开源分发 | 免费 |
| npm 私有（组织） | https://npm.pkg.github.com | GitHub Organization | $7/mo 起 |
| Verdaccio 自建 | http://localhost:4873 | 内网隔离 | 自建维护 |
| Arborist/Proget | 企业级 | 企业内网 | 商业许可 |

**建议**：L0 free 层走公开 registry；L1/L2 付费层走私有 registry（`@motor-ai/l1`、`@motor-ai/l2`）。

## 发布脚本（已就绪）
```bash
cd motor-ai-l0
npm publish --dry-run    # 预览打包内容
npm publish              # 正式发布的（需 token）
```

## package.json 当前状态
```json
{
  "name": "dsh-motor-ai-l0",
  "version": "0.1.0",
  "type": "module",
  "main": "./index.mjs",
  "files": ["index.mjs", "cordis.patch.yml", "lib/", "tools/", "benchmarks/", "skills/", "models/", "scripts/", "CHANGELOG.md", "README.md"],
  "license": "MIT",
  "dsh": { "compatibility": { "dsh": ">=0.1.2 <0.2.0", "node": ">=22.0.0" } }
}
```
✅ files 白名单已含 skills/ models/（G2 已补）
✅ 无 private 标记（可公开 publish）
⚠️ 无 `repository` 字段（建议补 GitHub URL）
⚠️ 无 `bugs` 字段（建议补 issue tracker URL）

## 发布前最后检查
```bash
# 1. 打包预览
npm pack --dry-run

# 2. 验证三元一致性
node scripts/release.mjs --plugin dsh-motor-ai-l0

# 3. 运行质量门
node scripts/regression.mjs

# 4. 运行物理规则校验
node scripts/verify.mjs

# 5. 真源同步检查
python knowledge-sync/sync-constants.py --check
```

## 发布后维护
- 版本号遵循 SEMVER（当前 0.1.0 属 pre-1.0 阶段，breaking change 可升 minor）
- CHANGELOG.md 每版必更
- yanking：紧急修复时可 `npm yank motor-ai-l0@0.1.0` 下架问题版

# AppRank Index — 应用榜单 SEO 站生成器

把 `e-hai/Appstore_Top_30` 的开放榜单数据，转成**合规、可全自动更新、零成本**的海外 SEO 静态站。

## 数据源

- 仓库：https://github.com/e-hai/Appstore_Top_30
- API：`https://e-hai.github.io/Appstore_Top_30/api/`（CORS 全开、无需鉴权、GitHub Pages CDN）
- 覆盖：39 个国家/地区 × App Store / Google Play × 免费/付费/畅销榜 × 100+ 天历史
- 上游每日北京时间约 08:00 更新（其 GitHub Actions）

## 为什么这样设计（合规要点）

| 要求 | 实现 |
|---|---|
| 每页有真实原创内容 | 洞察段由当日真实数据计算（新进榜、最大升幅、头部开发者、品类分布），非变量替换 |
| 避免 thin content | 每页含方法论、7d 排名变化、趋势 SVG、交互筛选器、来源归因 |
| schema 与可见内容一致 | `Dataset` + 前 3 名 `MobileApplication`（含可见评分）；**不做 FAQ schema**（2026 已停用） |
| 国际化合规 | hreflang 用 en-XX 地区变体；中国三地区显示"中国台湾/中国香港/中国澳门"，hreflang 用 zh-Hant-TW/HK/MO |
| 单站、可清理 | 全站一个域名；无价值页可在下架清单中 noindex/删除 |
| 免费全自动 | 仅用 Python 标准库 + GitHub Actions（公开仓库免费）+ Pages 托管 |

## 本地运行

```bash
python3 src/generate.py --out site --sample   # 先跑 10 页样本
python3 src/generate.py --out site            # 全量
```

## 三种运行模式

```bash
# 1) 样本：10 页，人工逐页审读（上线第一步）
python3 src/generate.py --mode sample --out site --no-indexnow

# 2) 全量：总榜 + 游戏榜，约 460+ 页（实测 2026-11-20）
python3 src/generate.py --mode full --out site --no-indexnow

# 3) 增量：每日运行，只重写实质变化的页，并（可选）提交 IndexNow
python3 src/generate.py --mode full --out site --state .build_state.json
```

生成物在 `site/`，可直接用任意静态托管打开预览。

**全量规模说明（实测）**：数据源并非所有组合都有数据——总榜 2 平台 × 39 市场 × 3 榜几乎全覆盖；品类榜只有"游戏"有数据。因此全量只生成"总榜 + 游戏榜"（约 462 页），不生成指向空数据的页面（避免 thin content）。

## 上线步骤（Cloudflare Pages）

部署在 **Cloudflare Pages**（免费额度：500 构建/月、无限带宽、自带 `*.pages.dev` + 免费 SSL），不用 GitHub Pages。

1. **域名**：默认用免费域名 `https://apprank.pages.dev`（项目名 `apprank`）。要换成你自己的独立域名，见下方"换域名"。
2. 先人工逐页读 `site/` 下的 10 页样本，确认洞察段、数据、schema 无误（**这一步不能省**）。
3. 把代码推到 GitHub 新仓库（Actions 在此定时触发，见下）。
4. 在 Cloudflare 建 Pages 项目（见下方"首次部署"），拿到 Account ID 与 API Token，填进 GitHub Secrets。
5. 首次手动触发 workflow（`workflow_dispatch`），确认部署成功、页面能打开。
6. 上线后到 Google Search Console 提交 `sitemap.xml` 并验证域名所有权。

### 首次部署（一次性配置）

1. Cloudflare 控制台 → **Workers & Pages** → **Create** → **Pages** → **Connect to Git** 选中该仓库；或直接走下方 wrangler 部署（此时会由 wrangler 自动建项目）。
   - 项目名建议 `apprank`，则生产 URL 为 `https://apprank.pages.dev`。
   - 构建设置：本方案由 GitHub Actions 生成 `site/` 并用 wrangler 上传，Cloudflare 侧无需再配构建命令（若用 Connect to Git，把构建命令留空、输出目录设 `/`，避免重复构建）。
2. 获取 **Account ID**：Cloudflare 控制台右侧栏 / 或 `wrangler whoami`。
3. 创建 **API Token**：My Profile → API Tokens → Create Token → 用 "Edit Cloudflare Workers" 模板（含 Pages:Edit 权限）。
4. 在 GitHub 仓库 → Settings → Secrets and variables → Actions，添加：

| 类型 | 名称 | 值 |
|---|---|---|
| Secret | `CLOUDFLARE_API_TOKEN` | 上一步的 token |
| Secret | `CLOUDFLARE_ACCOUNT_ID` | 上一步的 Account ID |
| Variable | `CF_PAGES_PROJECT` | `apprank`（项目名，可改） |
| Variable | `SITE_BASE_URL` | `https://apprank.pages.dev`（换域名时改这里） |
| Secret | `INDEXNOW_KEY` | 可选，32 位十六进制 |

5. Actions → 点 "Run workflow" 手动跑一次。日志末尾出现部署 URL 即成功。

### 换独立域名（以后）

不用改代码、不用重新生成本地文件：在 GitHub 把 Variable `SITE_BASE_URL` 改成新域名（如 `https://www.yourdomain.com`），并在 Cloudflare Pages 项目 → Custom domains 里添加该域名。下一次每日构建会自动用新域名重写全站 canonical/hreflang/sitemap。DNS 按 Cloudflare 提示配置即可（托管在 Cloudflare 时通常自动）。


## 增量更新与 IndexNow（已实现）

`src/incremental.py` 实现了三件事：

1. **实质变化判定**：对每页的核心数据（app_id / 排名 / 排名变化 / 状态 / 评分 / 评分数）算指纹，与 `.build_state.json` 里的上一轮指纹比对。只有指纹变化或跨新数据日才重写页面——**同日无变化重跑会 100% 跳过，不产生"改日期伪装更新"**（这是 Google News 明令禁止、也不利于程序化站质量判定的行为）。
2. **IndexNow 提交**：只对"变化的 URL"批量 POST（单次 ≤10,000），处理 200/202/429 退避。需设置 `INDEXNOW_KEY`（32 位十六进制），并把 `{key}.txt` 部署到站点根目录（生成器会自动写入 `site/`）。**Google 不在 IndexNow 参与列表**，Google 收录仍靠 sitemap + GSC。
3. **GitHub Actions 集成**：workflow 用 `actions/cache` 跨运行保存 `.build_state.json`，从 `secrets.INDEXNOW_KEY` 读取 key，每日自动跑全量+增量。

验证结果（2026-11-20）：首次全量生成 462 页；同日第二次运行 462 页全部跳过、0 变化提交。

## ⚠️ 数据来源与授权提醒

1. **上游仓库没有 LICENSE 文件**——代码与数据的复用权限在法律上不明确。本方案只做"事实数据展示 + 归因 + 深链到官方商店"，不构成数据转售。若商业化，请先联系作者确认授权。
2. Apple 数据：官方仅授权"在自己的网站展示结果"，**未授权把 JSON 当独立数据集再发布/转售**；App 名称、图标、评分归各自所有者。
3. Google Play 无官方免费榜单 API；Android 数据依赖上游的非官方采集，稳定性有限。
4. 排名为**每日采样快照**，非实时；页面已显式声明这一点。
5. 涉及金融/医疗/安全类 App 会触及 YMYL，需补强作者资质与事实核查。

## 合规红线（务必遵守）

- 不把 `[应用名][国家]` 套进同一段模板批量出页；
- 不跨多域名复制同一批页面，不做模板页脚互链；
- 不伪造作者、不批量复用他人图标/截图作独立卖点；
- 任何人工关口（首轮 10 页审读、月度抽检、弱页清理）不可省略。

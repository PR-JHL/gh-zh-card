---
name: gh-zh-card
description: 把任何 GitHub 仓库变成一张中文速览卡（1080×1440 PNG）+ 一段可直接发布的中文文案。当用户说"这仓库是干嘛的""值不值得看""给这个项目做张图/卡片/笔记"，或要批量产出"今日 GitHub 精选"时使用。
---

# gh-zh-card

把英文仓库压成一张中文卡片，回答一个问题：**这东西值不值得我花时间。**

## 什么时候用

- 用户扔来一个 GitHub 链接，问「这个怎么样 / 能不能用 / 值不值得看」
- 用户要给自己的项目、或别人的项目做一张中文介绍图
- 用户要批量产出「今日 GitHub 精选」这类内容

## 工作流程（按顺序，别跳）

### 1. 先拿真数据

```bash
python gh_card.py --fetch owner/repo --payload p.json --out out/
```

- `--fetch` 直接从 GitHub API 取 `stars / forks / language / license / pushed_at`
- `--fetch` 会用真数据**覆盖 payload 里的旧统计**（文案字段不动），加 `--save` 再把数字和
  抓取时间写回 payload —— 之后 `copy_check.py` 才能离线复核这些数字
- **数字只能来自 API，或用户提供的已核实数据。** 禁止凭印象填
- 网络不通（代理重置等）时用 `--offline`，把核实过的数字写进 payload
- API 限流 60 次/小时（未带 token）：设 `GITHUB_TOKEN` 环境变量可到 5000 次/小时

### 2. 读 README，再写文案

只看仓库 description 写出来的卡一定是空话。要么读 README，要么读 `SKILL.md` / 文档首页。

### 3. 写 payload

```json
{
  "owner": "owner", "repo": "repo", "url": "https://github.com/owner/repo",
  "name_zh": "项目名（保留原名即可，别硬起中文名）",
  "tagline": "≤34 汉字，说清它让你能做到什么以前做不到的事",
  "pain": ["≤38 字×3，写具体的痛，不写功能"],
  "for_who": "≤30 字，写给一类人",
  "start": "怎么开始；是命令就不带 $ 前缀，是步骤就写人话",
  "verdict": "≤50 字，一句话结论",
  "stars": 2059, "language": "Go", "license": "Apache-2.0",
  "updated": "2026-11-13T09:00:00Z",
  "topics": ["最多 3 个，仓库真实 topics"]
}
```

字段全部可选，缺了就不渲染那一块。但 `owner/repo/name_zh/tagline` 缺了卡就是残的。

**写完先体检，别急着渲染：**

```bash
python copy_check.py p.json            # 文案 vs payload：数字 / 长度 / 夸大词 / 结构
python copy_check.py p.json --online   # 顺便打 API 抓实时数据核对（星数容易过期）
```

它只判断机械能判的部分：文案里的星数、推送天数、许可证跟 payload 对不对得上；tagline 有没有
真的翻成中文；痛点是不是写成了功能清单；长度会不会把卡片挤到缩字号/截断。

**「有没有说到点上」它管不了** —— 那要读 README，还是你的事。

### 4. 渲染

```bash
python gh_card.py --payload p.json --offline --out out/ --name 01-slug --md --caption
```

**选风格**（`--style`，默认 `obsidian`）：

| 内容类型 | 用哪个 |
|---|---|
| 开发者工具、代码类、技术文章 | `obsidian`（紫调、克制） |
| 硬核/科幻/机器人/航天，或重大发布 | `wandering-earth`（冰蓝 HUD、警示红条） |
| 需要更多风格 | `--style-dir /path/to/design-cards/styles` 后用 `--style <id>` |

**署名**：卡片底部默认有一行很淡的 `gh-zh-card · github.com/…`。**别关它**（`--no-credit`）——那和"把项目链接从卡片上撕掉"是一回事，是这个工具唯一的传播入口。

**风格不能乱配。** 一个桌面宠物项目套「流浪地球」会显得可笑；一个工业软件套柔光紫也不对。拿不准就用 `obsidian`。

产出：
- `out/01-slug.png` —— 卡片，1080×1440（小红书 3:4 / 公众号 / X 长图都能用）
- `out/01-slug.md` —— Markdown 版，可直接做 issue / 公众号正文
- `out/01-slug.caption.txt` —— 自媒体文案 + hashtag

### 5. 自检（必做）

先跑脚本自检，再亲眼看一遍图。

```bash
python gh_card.py --payload p.json --offline --out out/ --check
```

`--check` 把原来只能肉眼扫的问题变成断言：文字出界、压到页脚数据条、两块文字互相压住、
单行超宽、豆腐块（字体缺字形）、对比度不足、内容被省略或截断。有 error 时退出码 1。

**内容太长时脚本会自动降级**（整体缩字号 → 省掉命令块/适合谁 → 省第 3 条痛点），
每一步都会在 `--check` 里报出来。看到 `COPY_DROPPED` / `COPY_TRUNCATED` 就**改文案**，
不要改排版参数硬塞。

**「文案的问题」用 `copy_check.py` 管（渲染前），「版面的问题」用 `--check` 管（渲染后）。**

**然后看图，只检查机器判不了的东西：**

- 文案读起来是不是人话、有没有硬吹（见下面的「文案质量标准」）
- 这套风格配不配这个项目
- 页脚四个数据项有没有互相贴住（自检也会提醒）
- **深色卡是不是"发闷"**：整张图除了文字几乎没有明暗过渡，说明 `glow` / 渐变没配好，换风格或调 JSON

发现问题就缩短文案，不要改排版参数硬塞。

## 文案质量标准

这张卡好看与否，90% 取决于文案，不是排版。

| 字段 | 要写成这样 | 不要写成这样 |
|---|---|---|
| tagline | 让任何 AI agent 隔着电脑操作你的手机，每一步你都能插手 | 一个基于 MCP 的手机自动化代理框架 |
| pain | 手机上最烦的是重复动作：点确认、翻列表、回消息 | 支持多点触控与手势录制 |
| for_who | 想让 AI 接管手机重复劳动，但不敢完全交出去的人 | 开发者 |
| verdict | 手机自动化方案里，把「人随时能接管」做得最认真的一个 | 史上最强手机自动化工具 |

**禁止**：把 README 的功能清单直接翻译过来。**禁止**：用「最强 / 第一 / 颠覆」这类可被当场证伪的词。

## 反面教材

```
tagline: "An MCP-based framework for phone automation"   ← 没翻译，等于没做
pain:    ["支持截图", "支持点击", "支持滚动"]              ← 这是功能表，不是痛
verdict: "绝对是你见过最好的"                              ← 吹，且无法验证
```

## 局限（要主动告诉用户）

- 它不判断代码质量 —— 没跑过测试就别替项目背书
- 它不做技术翻译 —— 重点回答「该不该看」，不是「原文说了什么」
- 需要本地有中文字体（Windows 微软雅黑 / macOS 苹方 / Linux Noto CJK）
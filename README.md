<div align="center">

# gh-zh-card

**30 秒看懂一个 GitHub 项目 —— 一张中文卡片，替你读完 README**

简体中文 · [English](#english)

![Python](https://img.shields.io/badge/python-3.9%2B-blue)
![deps](https://img.shields.io/badge/deps-Pillow%20%2B%20requests-lightgrey)
![license](https://img.shields.io/badge/license-MIT-green)

</div>

## 问题

GitHub 热榜每天几十个项目。点进去：英文 README 三千字，翻到底才发现——**这个我用不上。**

`gh-zh-card` 把它压成一张图：它到底让你能做到什么、解决哪个具体的痛、适不适合你、怎么开始、数字是不是真的。

<p align="center">
  <img src="examples/01-pinky-obsidian.png" width="46%">
  &nbsp;
  <img src="examples/01-pinky-wandering-earth.png" width="46%">
</p>

<p align="center"><sub>同一份内容，两种风格：<code>--style obsidian</code> / <code>--style wandering-earth</code></sub></p>

卡上的数字全部来自 GitHub API，不是手填的。

## 它是什么

一个 **agent skill**（Claude Code / Codex / Cursor / DSH 都能用）+ 一个零额外依赖的渲染脚本：

- `gh_card.py` 只做两件硬事：**从 GitHub API 取真数据**、**把排版渲染成 1080×1440 PNG**
- 中文文案由 **agent 写**（它读过 README），不是机器翻译 —— 这是它和「翻译插件」的根本区别
- 数字不接受手填：`--fetch` 直接打 API；`--offline` 模式要求你写已核实的数字

## 安装

```bash
git clone https://github.com/PR-JHL/gh-zh-card
cd gh-zh-card
pip install pillow requests   # 大多数 Python 环境已经有
```

装成 skill：把整个目录放进你的 agent 的 skills 目录，或直接让 agent 读 `SKILL.md`。

## 用法

**给 agent 用（推荐）** —— 把 `SKILL.md` 交给它，然后说：

> 帮我看下 DIYgod/pinky 这个项目，做一张中文速览卡

**自己用手** —— 抓数据 + 渲染：

```bash
# 1) 先拿真实统计（会写进 payload 里已有的字段之上）
python gh_card.py --fetch DIYgod/pinky --payload payloads/pinky.json --out out/ --name pinky

# 2) 或者完全离线：数字已核实过
python gh_card.py --payload payloads/pinky.json --offline --out examples/ --name 01-pinky --md --caption
```

三条产出：

| 文件 | 用途 |
|---|---|
| `*.png` | 1080×1440 卡片，小红书 3:4 / 公众号 / X 长图通用 |
| `*.md` | Markdown 版，做 issue、公众号正文 |
| `*.caption.txt` | 自媒体文案 + hashtag |

## 风格

卡片外观完全由 `styles/<id>.json` 决定，改配色不用碰代码：

```bash
python gh_card.py --payload payloads/pinky.json --offline --out examples/ --style wandering-earth
python gh_card.py --payload payloads/pinky.json --offline --out examples/ --style obsidian
```

**自带 44 套风格**（28 套纸感印刷 + 16 套风光片）：

![44 套风格总览](docs/styles-gallery.jpg)

| 家族 | 套数 | 色彩从哪里来 | 适合 |
|---|---|---|---|
| `gen1-print` 第一代 · 纸感印刷 | 28 | 油墨、纸张、矿物颜料 | 印刷物、复古、严肃文档、文化题材 |
| `landscape-photo` 风光片 | 16 | 自然光、大气散射、水面反射 | 自然/旅行/摄影/需要"呼吸感"的封面 |

```bash
python gh_card.py --list-styles                    # 看全部 44 套
python gh_card.py ... --style lofoten-aurora       # 罗弗敦极光（墨绿夜空 + 极光弧）
python gh_card.py ... --style sossusvlei-dune      # 纳米布沙丘（铁锈红 + 钴蓝）
python gh_card.py ... --style swiss-international  # 瑞士国际主义（最稳的一套）
```

风光片家族的规则是「**竖向渐变就是画面本身**」：`bg_top` 是天空上沿，`bg_bottom` 是地平线或水面。所以它们不是"换了颜色的纸"，是另一种东西。

一个风格 JSON 只需要三块：`palette`（12 个颜色）、`decor`（网格/扫描线/四角标/外框/顶部色带/柔光/压暗）、`type`（标题倍率、行高、等宽开关）。

**想加自己的风格**：复制一个 JSON 改颜色即可；或者用 `--style-dir <目录>` 加载外部风格库。

设计约束只有两条：**不许纯黑死卡**（深色必须有柔光或明显渐变，否则就是"没希望的黑"），**不许 AI 默认审美**（判据：这个配色放到任何 AI 生成的落地页上都毫无违和，那它就是废的）。

重新生成总览图：`python gallery.py`

## payload 字段

| 字段 | 必填 | 说明 |
|---|---|---|
| `owner` / `repo` / `url` | ✓ | 仓库标识 |
| `name_zh` | ✓ | 项目名，保留原名即可 |
| `tagline` | ✓ | ≤34 汉字，说清它让你能做到什么 |
| `pain` | | 3 条，≤38 字/条，写具体的痛 |
| `for_who` | | ≤30 字，写给一类人 |
| `start` | | 怎么开始；是命令就不带 `$` |
| `verdict` | | ≤50 字，一句话结论 |
| `stars` / `language` / `license` / `updated` | | `--fetch` 自动填；`--offline` 时须已核实 |
| `topics` | | 最多 3 个，用仓库真实 topics |

## 署名

生成的卡片底部有一行很淡的

```
gh-zh-card · github.com/PR-JHL/gh-zh-card
```

这是这个工具唯一的传播方式——**卡片被转发时，链接跟着走**。不想要就加 `--no-credit`。

## 设计原则

1. **数字必须真。** 卡片上唯一能被一眼验证的就是 stars 和 license，编一次就没人信了。
2. **宁可不说，不要硬吹。** `verdict` 里不出现「最强 / 第一 / 颠覆」。
3. **一张图要能替代点进 README。** 看完还不知道自己该不该用它，就是没做好。

## 局限

- 不判断代码质量 —— 没跑过测试就不替项目背书
- 不做技术翻译 —— 回答「该不该看」，不是「原文说了什么」
- 需要本地中文字体：Windows 微软雅黑 / macOS 苹方 / Linux Noto CJK
- 排版只支持中文 + 英文混排；纯日文/韩文可能缺字形

## English

`gh-zh-card` turns any GitHub repo into a 1080×1440 Chinese quick-look card plus a ready-to-post caption.

The Python script does the two things that must not be fudged: it pulls real stats from the GitHub API, and it renders the layout. The Chinese copy is written by your coding agent **after it reads the README** — that is the difference between this and a translation plugin. It answers one question: *should I spend time on this repo?*

```bash
python gh_card.py --payload payloads/pinky.json --offline --out examples/ --name pinky --md --caption
```

Requires Python 3.9+, Pillow, requests, and a local CJK font (Microsoft YaHei / PingFang / Noto CJK).

## License

MIT
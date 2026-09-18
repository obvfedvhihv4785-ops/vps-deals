ILANG
TYPE:agents PROJECT:vps-deals-promo-radar LANG:zh

# ============================================================================
# 给以后接手这个仓库的 AI 看的文件。
# GitHub 上的 AI 编程工具会优先读 AGENTS.md，所以这份文件比 README 更常被机器读。
# 它用 I-Lang 写，因为它比 yaml 多表达一样东西：边界和禁止。
# ============================================================================

::STATE{@PROJECT,
  what:一个自动更新的 VPS 优惠细分站,
  how:纯Python管线 定时抓公开定价页 生成静态站 部署到Cloudflare Pages,
  runtime_cost:零,
  runtime_deps:无,
  runtime_api_keys:无,
  runtime_inference:无}

::STATE{@DATA,
  origin:各家厂商自己的公开定价页或优惠页,
  never_origin:其他比价站 二手数据 用户提交 人工录入,
  access:纯HTTP GET 只读 遵守robots.txt 不绕反爬 不抓登录后内容}

::MODULE{PIPELINE|title:管线四步 顺序不能反}
  1 | scraper.py | 读 .ilang/site.ilang 的厂商清单 抓公开页 抽价格 写 data/offers.json
  2 | build.py   | 读 data/offers.json 和 .ilang/site.ilang 渲染 templates/ 到 site/
  3 | .github/workflows/update.yml | 每6小时跑一次 跑完把变化 commit 回仓库
  4 | Cloudflare Pages | 从本仓库构建 site/ 输出到 pages.dev

::MODULE{CONFIG|title:唯一真源}
  .ilang/site.ilang | 品牌 细分 域名 厂商清单 抓取入口 字段 渲染开关 全部在这里
  ::RULE{加一家厂商 只改这个文件的 PROVIDERS 一行 不许改代码}
  ::RULE{换品牌或换细分 只改这个文件的 STATE 一行}
  ::RULE{scraper.py 和 build.py 必须真的读它 不许在代码里再写一份清单}
  ::CHECK{改 site.ilang 里任意一项 重跑管线 站上就该变 变不了就是配置做成了摆设}

::MODULE{ALLOWED|title:允许的动作}
  - 增加或删除 PROVIDERS 里的厂商行
  - 调整 SETTINGS 里的抓取间隔 超时 延迟 价格上下限 首页条数
  - 用 RENDER 里的开关关掉某一类页面
  - 改进 scraper.py 的价格识别规则 让它更准
  - 改进 templates/ 里的排版和文案
  - 增加新的结构化数据类型 只要数据是真的
  - 给某个厂商填上 affiliate_url 前提是该厂商公开条款允许

::MODULE{FORBIDDEN|title:绝对不许做}
  ::BOUNDARY{never:手写或估算任何价格 折扣 佣金|scope:permanent}
  ::BOUNDARY{never:抓不到数字就编一个填上 或拿别家的价格代替|scope:permanent}
  ::BOUNDARY{never:绕反爬 抓登录后内容 伪造User-Agent冒充真人|scope:permanent}
  ::BOUNDARY{never:给抓不到价格的条目生成 Offer 结构化数据|scope:permanent}
  ::BOUNDARY{never:刷量 买粉 买流量 自买自推|scope:permanent}
  ::BOUNDARY{never:把结构化数据里的字段填成编的值去凑富媒体摘要|scope:permanent}

::MODULE{RULES|title:改代码时必须守的}
  ::RULE{scraper.py 和 build.py 只准用 Python 标准库 不许引入第三方依赖}
  ::RULE{管线里不许调用任何模型 不许读环境变量里的密钥 除部署那一步}
  ::RULE{抓取失败必须如实记成 blocked 或 error 不许包装成"没有优惠"}
  ::RULE{重抓失败时 保留上一次已验证的价格 但必须标记 stale 并写明验证日期}
  ::RULE{stale 记录的日期一律写"上次成功读到的时刻" 不许写这次失败的尝试时刻 卡片徽章和证据出处都不许}
  ::RULE{卡片徽章不许只因为 show_price 就写 verified 必须按状态出 失败的那次抓取不算 verified}
  ::RULE{给价格配的候选清单和价格同生共死 价格被 carry forward 时清单也必须跟着 carry forward}
  ::RULE{标题承诺了要列的东西 下面就必须真有 空表配承诺的标题一律算 bug}
  ::RULE{同一页上任何"已验证"字样都必须由状态推导 不许由"有没有价格"推导 一行里同时出现 verified 和 stale 一律算 bug}
  ::RULE{结构化数据里的 InStock 是对当下的断言 stale 记录必须在同一个 JSON-LD 块里写明"这是上次验证的值 不是当前读数" 别指望搜索引擎会去读正文}
  ::RULE{valid_until 已过的优惠 保留记录但必须标成过期 不许当有效展示}
  ::RULE{每个价格必须带 price_evidence 原文和 source_url 和 fetched_at 缺一不可}
  ::RULE{价格附带承诺期或首期促销时 必须把条件一起显示 只写 /mo 不写条件是误导}
  ::RULE{承诺期只能从紧邻价格的原文里读 读不到或页面给了多个档就写"未注明" 不许猜}
  ::RULE{页面说"所有方案都需预付 月费是总价除以月数"时 全页每个 /mo 都是预付摊销 不是月付 必须按预付标注}
  ::RULE{厂商写了续费价时 必须和首期价一起显示 只登首期价等于只登了半张报价单}
  ::RULE{续费价必须高于首期价 低于或等于就不算提示 不许为了警示而登}
  ::RULE{续费那句话和价格之间若还夹着别的价格 那句话多半属于下一档方案 不许拿来配这个价}
  ::RULE{续费价与首期价币种不同时 一个都不许登 连"仅供参考"都不许}
  ::RULE{厂商没写承诺期时 写"未注明" 不许写"按月付费" 页面沉默不等于月付}
  ::RULE{改完必须本地跑通 scraper.py 和 build.py 再提交}
  ::RULE{改完抽取规则必须跑 tests/test_extraction.py 三个陷阱用例一个都不许红}

::MODULE{FILES|title:每个文件的职责边界}
  ilang.py       | I-Lang 配置解析器 只读配置 不抓不渲染
  scraper.py     | 抓取和抽取 不碰 HTML 模板
  build.py       | 渲染和生成 不发起网络请求
  templates/     | 只做展示 不许自己算价格
  tests/test_extraction.py | 抽取规则的回归测试 用的是各家页面原文 改规则必须让它全绿
  data/offers.json | 由 workflow 每次覆盖 不要手工编辑
  data/page_state.json | 每页内容哈希 + 该内容最后变化的日期 用来算真实的 sitemap lastmod 不要手工编辑
  site/          | 构建产物 由 build.py 覆盖生成 本轮没写到的孤儿文件会被清掉 不要手工编辑

::MODULE{CONTACT|title:有疑问时}
  ::ASK{拿不准某个价格该不该收录 就不要收录 宁缺勿造}
  ::ASK{拿不准某条数据能不能用 先看它的 source_url 是不是厂商自己的公开页}

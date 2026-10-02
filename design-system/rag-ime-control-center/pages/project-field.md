# Project Field 高保真原型合同

> 状态：可点击原型，不是生产领域模型
> 基线：`084fefb feat: refine Room activity and recovery`
> 记录日期：2026-08-05
> 历史资料整理截止：2026-08-04 23:59:59 +08:00

> 维护说明（2026-10-02）：下文保留当时的需求与原型决策。独立 Project Field
> 页面及其 viewport 实现已退出产品；当前入口、状态和扩展职责见
> [架构说明](../../../ARCHITECTURE.md)。`react-zoom-pan-pinch` 与 `d3-contour`
> 已不再是产品的直接依赖，以下取舍不表示当前运行时仍加载它们。

## 用户原始需求与愿景（逐字保留）

> “我做这个的原因就是room，项目，这些不好找。一个项目多个tik和room”

> “每个room又可以产生记忆。可以借鉴wayfinder。”

> “其实也不用分上几个 room 呀，就一个 room 也能解决吧。或者 能够并行吗？并行的话，这些 Room 之间能够沟通吗？”

> “UI不用解释这么多，一内容为主，直观，甚至偏抽象的ui。”

> “Project 本身成为一个长期存在的可视化操作空间，Room 成为其中持久存在的目标与上下文对象。”

> “第一版只做一个高保真可点击”

> “界面bug很多，而且不是岛。因为一个room可能负责了不止一个功能”

> “无法拖动，而且一个可以更ui炫酷，动画更多，结合wayfinder。”

> “当前这个岛用户还是看不出每个room完成哪些方面”

> “对于这个例子，我们得先按照wayfinder和开发历程，来还原一个用当前技能流的正确的岛。”

> “我们可以模拟wayfinder干完的所有文档”

> “完全不美观，持续迭代ui”

> “内容还是当前内容为主”

> “weyfinder是架构为主还是纵向验证为主，我们岛是按架构来吗”

> “参考这个优化ui”

> “美化。加特效”

> “非常杂乱”

> “你读docs，生成还原的数据了吗，我准备用这个接入真实项目了”

> “用docs整理，这些本来也是真实数据。还可以用ceodexx这十几天的聊天记录。caludecode，pi，omp这些agent了聊天记录。docs一同还原一组我可以演示的图”

> “personal-agent-workbench-main-release还是这个分支”

> “按照wayfinder和技能流整理好docs了吗，到8-4的各自聊天记录和文档。用luna max整理”

> “不规整”

> “稀疏一些，还要进度”

> “不要时间，太丑了”

上面的文字是需求锚点。AI 可以在下面解释、归纳和修订方案，但不能改写或覆盖这些原话。

## 这次原型要证明什么

现有侧栏把对话、多人协作、任务、文档和伙伴按系统功能分开。用户真正需要找的是“我在推进哪个项目结果、相关 Room 在哪里、现在是否需要我”。Project Field 把项目从文件夹变成长期存在的操作空间，把 Room 从聊天条目变成用户结果与上下文的持久对象。

本轮只验证交互形态，不验证数据库、模型整理质量或真实 Room Runtime。四个必须可点击验证的状态是：

1. Project 全景：低分辨率 Room 同时存在，当前航向清楚。
2. Room Focus：Room 在原位置语义变焦成工作区，退出后仍处于同一 Project。
3. Navigator：新输入在 Project Field 上原地路由，原始输入进入目标 Room，归属可撤销。
4. Background Attention：后台 Room 在原位置改变状态，但不切走当前前台 Room。

## 已确认的领域边界

### Project

- Project 长期存在。
- 原始愿景不可变；当前航向和阶段目的地可以版本化更新。
- Project Map 是低分辨率投影和索引，不是目标、记忆、执行或 Room 状态的 owner。
- 地图整理可以由模型自动完成，但必须有来源、版本和撤销能力。

### Room

- 一个 Room 对应一个用户管理的结果或航程，不对应一个 Spec、Issue、Ticket 或内部代码切片。
- Room 不按前端、后端、数据库或 Runtime 等架构模块划分。一个结果可以跨越多个架构层，并按可验收的纵向切片推进；架构范围只作为 Room 详情、证据和依赖投影。
- 一个 Room 在地图上是一张结果摘要纸；它可以包含多个相关功能方向、多个独立验证的执行切片，也可以由多个 Agent 并行推进。
- 功能方向是 Room 的内部投影，不会因为一个 Room 同时负责三个功能，就在项目地图上拆成三个 Room。只有目标、上下文和验收结果真正独立时，才创建关联 Room。
- `workflow` 表示这个 Room 的交付阶段；它和 Room 内负责的功能方向是两个维度，不能把需求对齐、实施、验证等阶段画成功能节点。
- Gateway 每次默认只继续、重开或创建一个 Room。只有两个结果确实独立且值得同时推进时，才经用户确认创建后台 Room。
- Room 是上下文、运行和记忆边界；地图上的地标只是 Room 的可视化，不拥有第二份状态。
- `完成` 不等于 `归档`。完成后的 Room 继续留在项目场；只有用户主动归档后才进入历史航线。
- 归档后再次出现同一目标时重开原 Room；形成新的独立结果时创建关联 Room。

### Gateway / Navigator

- Navigator 是一次性路由器，不是常驻聊天或特殊 Room。
- 点击已知 Room 不调用模型、不重新摘要、不创建 Session，只进行界面聚焦。
- 新输入先保存为项目级待归属需求。Navigator 只问到 Room 边界足够清楚，然后退出；完整需求对齐在 Room 内继续。
- 自动路由必须向用户显示建议目标，并提供撤销归属。

### Wayfinder 与 Room 的关系

- Wayfinder 是项目意义与空间定位的低分辨率地图，负责连接初始愿景、结果型 Room、当前交付状态和最终愿景；它既不是系统架构图，也不是实施阶段流水线。
- Matt 原始工作流中的 decision ticket 解决一个待定问题，并不等于本产品的一座 Room。Room 是更长期的用户结果与上下文边界；一个 Room 内可以多次对齐、规划、实现和验收。
- Project Field 按结果型 Room 组织。纵向验证是 Room 内的交付策略：从用户结果穿过必要的界面、服务、记忆与真实验收边界，直到形成可验证闭环。
- 路径表示项目结果如何分叉、组合与演进；架构依赖默认不成为全局主线，只在聚焦 Room 后按需显示。
- 汇聚是路径关系，不是独立节点。图上不创建额外的门、Room 或完成状态；所有必需 Room 验收后，路径才自然连接最终愿景。

### 并行与跨 Room 沟通

- 默认只有一个前台 Room。后台 Room 静默运行，通过原位置状态和 Attention Drawer 被感知。
- 普通后台阻塞与完成进入收件箱；改变目标、高风险权限或跨 Room 决策冲突才请求用户介入。
- 已声明依赖的 Room 可以自动交换结论、接口、产物和验证结果，但只读取有界交接摘要，不读取对方完整聊天。
- 如果交接会改变接收 Room 的原始目标、验收标准或项目航向，必须询问用户。

### 记忆

- 用户原始需求逐字保存在 Room 的需求锚点中；模型解释和阶段总结单独版本化。
- Project Field 可以由模型提出带出处的布局、关联和摘要候选；应用后必须可撤销，Project / Room 仍是状态 owner。
- 项目文档与 Room receipts 只作为地图的来源引用和项目知识，不自动进入个人 Memory 的 Evidence、Atom 或 Book。
- 个人 Memory 继续由独立的 capture、curation、promotion、rollback 与 retrieval owner 管理；地图与个人 Memory 可以互相导航，但不共享写入语义或生命周期状态。

## 信息加载合同

Project 打开时只需要 `ProjectFieldProjection`：

- 当前目的地与航向；
- 所有 Room 的稳定位置、目标名称、状态、最近有效结果和下一步；
- Room 间已确认关系；
- 是否需要用户；
- 待归属需求。

聚焦 Room 时增加 `RoomFocusProjection`：

- 原始目标；
- 当前结果；
- 已确认决定；
- 当前阻塞；
- 最近成果、下一步和工作流摘要。

只有真正工作或检查历史时才加载完整时间线、内部任务、Agent 活动、工具记录和证据。所有 Room 的“共在”指低分辨率投影共在，不是把所有聊天和技术事件同时放入 DOM 或模型上下文。

## 视觉与交互合同

本页继承 [`../MASTER.md`](../MASTER.md)，不建立第二套品牌系统。

- Project Field 使用现有冷白纸面、石墨文字和克制青绿焦点色。
- 不用海盗插画、星空粒子、游戏化宝箱或满屏霓虹。项目演进感来自起点、路径、目的地和空间记忆；默认场景不使用呼吸光、掠光、路线辉光或无限流动。
- 左侧 Project Rail 只负责切换完整项目世界；对话、任务、伙伴和文档是 Room 内部投影或检查器，不和 Project 竞争一级导航。
- 所有 Room 同时存在；当前 Room、需要用户的 Room 和普通背景 Room 通过悬边、文字、图标与边框建立层级，不通过改变纸张尺寸制造高低等级。
- Room 使用与 Control Center 一致的冷白纸张阅读面、1px 边框、5px 圆角和克制阴影。纸张保持规整，不通过随机形状或纹理制造空间感。
- 七张结果型 Room 纸张等宽等高，由真实历史决定从左到右的拓扑位置；列间与上下分支保留明显空白，页面缩放后也不能互相压住。总览不显示时间轴、日期刻度或卡片日期，日期只在详情与来源 receipts 中按需读取。当前 Room 以青绿色左悬边突出；需要用户的 Room 使用文字、图标和 Warning 状态；其他 Room 不铺大面积状态色。
- `真实使用与发布` 是贯穿项目底部的长期验收轨道，不是与普通 Room 并列的第八张末尾卡片；它保存真实输入、Runtime、Room 治理、工作流和记忆治理等验收检查点。
- 每张纸在未展开时显示标题、整理后的需求、自然语言阶段和验收证据进度；证据进度由 `Observable acceptance` 的已核验项计算，不从 `currentStage` 推算。五段交付链只在 Room Focus 中显示。
- 默认只画有来源回执的 `refines` 与 `led-to` 历史主线；`requires` 依赖在聚焦后按需显示，不把全部关系升级为技术流程图。
- 最终愿景是独立的未接通纸张。汇聚是路径关系，不是独立节点；未完成时不绘制任何 Room 到目的地的连接。
- Room Focus 不是 Modal 或新页面。纸张在原位置展开，周围 Room 保留为低密度空间摘要。
- `Escape` 或点击空白退出 Room Focus；草稿与聚焦状态按 Project 保留。
- `Command/Ctrl + K` 聚焦 Room 搜索。
- 现有 Room 位置默认不自动移动；模型只能建议新 Room 位置或局部整理，变更必须可撤销。
- 所有图标使用 Lucide；状态同时使用文字，不依赖颜色。
- 动效只表达镜头与对象连续性，尊重 `prefers-reduced-motion` 和应用减少动效设置。
- 缩放低于可读阈值后进入语义远景，只显示 Room 身份、需求短句或阶段，不把同一份微型正文机械缩小。
- 空白画布支持直接拖动，滚轮或触控板支持缩放；拖动画布不能误触 Room，Room Focus 中的滚动也不能被画布缩放抢走。
- 地图不暴露 Issue、Ticket、内部实施切片、助手推理或诊断术语；完整决定依据、技能流、开发轨迹和来源只在 Room Focus 中按需展开。

## 文档驱动的当前 Room

`Room 导航与项目图谱` 使用
[`../prototypes/room-navigation-wayfinder/README.md`](../prototypes/room-navigation-wayfinder/README.md)
中的 Wayfinder 回顾文档包，以及
[`../prototypes/room-navigation-wayfinder/real-project-reconstruction/README.md`](../prototypes/room-navigation-wayfinder/real-project-reconstruction/README.md)
中的真实来源清单。文档先于 UI 建立：

1. AlignmentDecisionPacket 固定用户原话、目标、边界与“无独立汇聚节点”的确认。
2. Wayfinder Map 索引初始愿景、最终愿景、八个结果型 Room、历史结构、贯穿式验收轨道和带回执关系；历史只决定拓扑，不在总览形成可见时间轴。
3. 每个 Room 文档保存整理后的需求、问题、已确认决定、出现时间、验收证据进度、当前交付和来源。
4. ImplementationPlan 和 WorkDocument 接续实施；质量门与独立复核保持 pending，不预填通过结论。

纸张网络只是这些文档的低分辨率投影。当前原型仍由 `reconstructed-personal-agent-project.ts` 持有单一运行时状态；浏览器不会解析 Markdown，也不会由文档建立第二个状态 owner。

### 开源前端取舍

本轮核对了 [React Flow](https://reactflow.dev/)、[tldraw](https://github.com/tldraw/tldraw)、[AFFiNE](https://github.com/toeverything/AFFiNE)、[BlockSuite](https://github.com/toeverything/blocksuite)、[D3 contour](https://d3js.org/d3-contour/density)、[Motion](https://motion.dev/docs/react-svg-animation)、[PixiJS](https://pixijs.com/8.x/guides/components/renderers) 与 [Paper.js](https://paperjs.org/reference/path/)：

- React Flow 的平移、缩放、选择和自定义 React 节点成熟且为 MIT，但默认领域是可编辑节点图。直接接入会把 Room 重新塑造成等权节点。
- tldraw 的无限画布与运行时 API 成熟，但 SDK 的生产使用需要许可证；它的默认工具、选择和白板对象也不是本产品的用户语义。
- AFFiNE / BlockSuite 证明了文档内容可以和无边画布融合，但它们是一套包含 block editor、CRDT 与协作状态的完整编辑器基础设施。本原型不应引入第二个内容与状态 owner。
- 当前仓库已经通过 `@antv/g6` 实现关系画布的拖拽、缩放和聚焦，但 Project Field 不复用它的图编辑器状态。`react-zoom-pan-pinch` 只作为受控 viewport 适配器，补齐 DOM 内容的拖动、滚轮/触控板缩放与语义镜头复位；Room DOM、语义缩放和权威状态仍由本产品持有。
- G6 的 donut node 与 `d3-contour` 曾用于探索多段状态和不规则轮廓，但纸张 Wayfinder 不采用这套视觉语义；它们只保留给其他既有关系图或历史原型，不参与当前 Room 投影。
- Motion 已经是本仓库的动效基础。默认场景不运行连续装饰动效；当前 Room 依靠纸张面积、内容密度和对比度建立层级。未来只在归属、聚焦或项目位置刚发生变化时使用一次性反馈，并完整服从 `prefers-reduced-motion`。
- PixiJS 和 Paper.js 能提供更完整的渲染 / 路径场景，但首版会引入独立场景树，并增加文本、键盘访问和状态同步成本，因此暂不采用。若真实数据达到数百个同时可见 Room，再用性能测量决定是否增加 Canvas/WebGL 低分辨率层。

因此本轮的运行时组合是 `现有 DOM 纸张组件 + react-zoom-pan-pinch + 现有 Motion`。没有复制开源项目的节点视觉或编辑器领域模型，也没有新增第二个渲染器或动效 owner。

## 原型数据与证据边界

Personal Agent Workbench 场景以 `.worktrees/personal-agent-workbench-main-release` 为唯一事实分支。2026-08-04 截止整理冻结在 `main@8a50a6b21ced` 加截止时工作文档：45 份历史项目文档、15 份当前 target draft、19 个去重后的主 Agent 会话、5 个 exact alias ref、1338 条用户消息和 9 个代表提交。经人工复核后，重建 manifest 选择与 8 个 Room 边界及 7 月起始时间线直接相关的 7 份文档、12 个会话和 16 个提交作为前端 receipts；完整索引与哈希位于 `reconciliation/wayfinder-luna-source-audit.v1.json`。

1. 输入体验闭环
2. 受控项目记忆
3. 统一产品工作台
4. Room 协作交付
5. Agent 长任务连续性
6. 工作流与能力收敛
7. Room 导航与项目图谱
8. 真实使用与发布

这不是对 827 次提交逐条重建，也不是生产 Room 数据导出。用户原话和当前项目文档决定结果边界；主 Agent 会话补足需求变化与实际过程；Git 提交只作实现旁证。没有同代真实验收的功能不会因为代码存在就标为完成。

`gpt-5.6-luna` 以 `max` reasoning 只生成结构化整理候选。候选经过 schema 校验后仍被人工发现两组 exact alias 的 canonical ID 对调，因此模型输出没有直接写入权威 docs；人工复核纠正重复会话映射、保留冲突并确认四项 Project Field 决定不变。最新纸张 Project Field 的用户视觉接受仍是开放验收。

`Room 导航与项目图谱` 进一步建立完整 Wayfinder 回顾文档包，并把本轮真实来源清单投影为“还原依据”。这仍不是“历史上真实运行过 Wayfinder”或“生产 Project Registry 已接入”的声明。

另外两个 Project 只用于验证“切换世界并恢复现场”的交互，不代表新的生产数据。

## 本轮修正的错误模型

- 旧版把 Room 画成航程上的单个功能节点，实际造成了 `Room = 功能` 的一对一错觉；当前版本改为“一张结果摘要纸对应一个可独立验收 Room”。
- 旧版提供“当前航程 / 完整全景”双视图，既增加选择成本，也让 `overview` 密度与 Room Focus 产生冲突。当前版本保留一个空间视图，需要总览时直接缩放画布。
- 旧版常驻关系图例和全部依赖线，把用户注意力拉向内部关系类型。当前版本只强调真实历史主线，依赖在聚焦后按需显示。
- 旧版让阶段位置等同于完成比例。当前版本把自然语言阶段与验收证据进度分开，避免“进入实现执行就算完成 3/5”的错误结论。
- 旧版把真实使用与发布放成末尾卡片。当前版本把它改为贯穿式验收轨道，表达每个纵向需求都必须持续可验收。
- 旧版把汇聚做成一个额外组件。当前版本删除该组件：路径可以自然收拢，但目的地在所有必需 Room 验收前保持未接通。

## 本轮明确不做

- 不增加 `projects`、Project–Room 关系或地图布局数据库表。
- 不接真实 Room、Session、Work Document、Knowledge Promotion 或 Agent Gateway API。
- 不把静态 Navigator 匹配器描述成真实 LLM 路由。
- 不自动创建、归档或移动真实 Room。
- 不以截图、Mock 数据或单元测试声称生产行为已经完成。

## 如何运行

```bash
cd control-center-web
pnpm install --frozen-lockfile
pnpm dev
```

浏览器打开 `http://127.0.0.1:5173/#/project-field`。原型分支的根路由也会进入 Project Field；“现有工作台”可以返回原 Control Center。

聚焦验证：

```bash
pnpm exec vitest run \
  src/features/project-field/project-field.test.tsx \
  src/app/route-registry.test.ts \
  src/components/layout/AppShell.test.tsx
pnpm typecheck
pnpm build
```

## 可点击验收脚本

1. 打开 Personal Agent Workbench，确认 8 个结果型 Room 共存，并能在三秒内找到当前 Room。
2. 确认所有收起 Room 纸张只显示标题、整理后的需求和一个短阶段，不在总览堆叠决定、验收或完整交付轨。
3. 点击当前 Room，确认决定、问题、验收、来源和完整五段交付轨直接可见；`测试驱动实现` 只显示为实现执行的内层方法。
4. 按 `Escape` 后回到相同 Project Field，并确认其他 Room 的位置没有改变。
5. 在 Navigator 输入“删除后旧候选偶尔还会回来”，确认建议“输入体验闭环”。
6. 选择“继续并发送”，确认原始输入出现在 Room 内，并能撤销归属。
7. 选择“作为新目标梳理”，确认只生成“待归属需求”，没有自动创建 Room。
8. 聚焦当前 Room 后点击“演示后台 Room 状态变化”，确认当前焦点不变，另一个 Room 在原位置显示“需要你”。
9. 主动打开 Attention Drawer，确认它只是提醒索引，不复制第二份 Room 状态。
10. 在 Room 输入草稿，切换到另一个 Project 再返回，确认聚焦 Room 和草稿恢复。
11. 拖动画布、放大、缩小并复位；确认操作真实改变画布且不会误触 Room。
12. 切换系统减少动效，确认语义层级保留且无强制镜头动画。

原型通过后，下一阶段才根据验证结果定义稳定 Project identity、权威 Project–Room binding、可撤销布局投影和知识晋升适配器。

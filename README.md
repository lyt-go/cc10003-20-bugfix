# 指标口径与分析实验平台

面向业务指标定义、口径管理、聚合查询、分群分析、A/B实验设计、分析方案管理与数据服务发布的数据分析工程平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 指标口径 | `metric` | 指标定义 | 指标编号、指标名称、计算公式 |
| 聚合查询 | `queryboard` | 查询模板 | 模板编号、指标名称、维度字段 |
| 分群分析 | `segment` | 人群分群 | 分群编号、分群名称、筛选规则 |
| 实验设计 | `abtest` | 实验方案 | 方案编号、实验名称、对照组定义 |
| 分析方案 | `analysis` | 分析方案 | 方案编号、分析主题、数据集范围 |
| 看板配置 | `dashboard` | 看板页面 | 看板编号、看板名称、包含指标 |
| 数据服务 | `delivery` | 服务版本 | 服务编号、数据集名称、查询参数定义 |
| 漏斗分析 | `funnel` | 漏斗定义 | 漏斗编号、漏斗名称、步骤序列 |
| 同期群分析 | `cohort` | 同期群定义 | 群组编号、群组名称、划分事件 |
| 归因模型 | `attribution` | 归因规则 | 规则编号、归因类型、回溯窗口天 |
| 指标预警 | `alertconfig` | 预警规则 | 预警编号、关联指标、比较运算符 |
| 报表调度 | `reportsch` | 报表任务 | 任务编号、报表名称、包含指标 |
| 维度属性 | `dimprop` | 维度属性 | 属性编号、维度名称、属性名称 |
| 多维下钻 | `datadrill` | 下钻路径 | 路径编号、起始维度、下钻层级 |
| 预测模型 | `forecast` | 预测方案 | 方案编号、预测目标、算法名称 |
| 标签管理 | `tagmgr` | 标签定义 | 标签编号、标签名称、标签分类 |
| 洞察推送 | `insight` | 洞察报告 | 报告编号、报告标题、分析主题 |
| 数据验证 | `dataval` | 验证任务 | 任务编号、验证数据集、验证类型 |

## 指标预警 ↔ 指标口径闭环

预警规则必须绑定指标口径版本，两个模块的状态与事务在 `app/services/alertconfig.py`
和 `app/services/metric.py` 里闭环：

```text
预警规则：草稿 → 规则评审中 → 已生效 → 已关闭（终点）
            ↑ 撤回评审 / 冻结恢复 / 发起修订 ↓
指标口径：草稿 → 口径评审中 → 已发布 → 已下线
                 ↑ 回滚生成新版本 ↓
```

- 阈值、比较运算符只能在草稿里改；生效前必须绑定当前「已发布」的指标版本，阈值必须是合法数值，
  `>=` / `<=` 按含等于处理（取值恰好等于阈值也算命中）。
- 只有「已生效」的规则参与累计；关闭后不再累计、不再通知，连续周期停在关闭前的原始值，
  重复关闭会被拒绝。同一指标同时只允许一个已发布版本的规则处于生效状态。
- 发布新版本或下线指标会冻结仍绑定旧版本的规则；冻结会保留连续周期与历史，恢复时先回到草稿，
  重新提交评审后才可绑定新版本。已下线指标必须用「回滚版本」生成新的评审版本再发布。
- 换版、下线、批量冻结与规则冻结都在 `store.transaction()` 里完成，任一环节失败整体回滚，
  不会出现「指标已下线、规则却还待处理」的单侧状态。
- 追溯：`GET /api/alertconfig/{id}/trace` 同时给出规则、绑定的指标版本、版本状态、历史与通知；
  `GET /api/metric/{id}/detail` 给出指标版本追溯与关联规则状态。

预警列表支持 `sort_by` / `order` / `page` / `size` / `metric_version`：
每条记录带 `序号` 与 `序列位置`（第 N / 共 M 条），后续页仍能按指标版本定位同一条规则。

### 测试

```bash
cd backend
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests -q
```

覆盖：关闭后触发、阈值边界相等、指标换版、下线冻结、回滚再发布、重复关闭、
并发修改阈值、单侧状态回滚、分页序号与版本追溯。

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

领域：数据工程
子方向：数据分析与数据应用

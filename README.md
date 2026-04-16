# KMGroup 生产管理系统

KMGroup 生产管理系统是一个基于 FastAPI + PostgreSQL 的制造业生产管理平台，支持订单管理、生产进度跟踪、库存管理、出货记录、微信通知等功能。

## 功能模块

| 模块 | 说明 |
|------|------|
| 订单管理 | 订单增删改查、批量导入、计算材料需求 |
| 订单排产 | Gantt图可视化排产、甘特图缩放、Excel导出、日期范围120天 |
| 生产进度 | 工序状态管理、完工标记、ETA计算、每日产出统计 |
| 产品管理 | 产品档案管理、3D模型上传（STP/STEP → GLB）、8工序工时配置 |
| 仓库库存 | 库存查询、入库/出库、待电镀管理、半成品管理 |
| 出货记录 | 出货登记、批量导入、库存自动扣减 |
| 生产报表 | 生产日志记录、达标率计算、批量导入 |
| 进度查询 | 图号搜索、综合信息查询（订单+库存+生产进度） |
| 微信集成 | 企业微信消息交互、自定义菜单、每日汇总推送 |

## 技术栈

- **后端框架**: FastAPI + SQLAlchemy (异步)
- **数据库**: PostgreSQL + asyncpg
- **任务调度**: APScheduler（每日10点发送汇总）
- **认证**: HMAC-SHA256 签名会话（12小时TTL）
- **密码加密**: PBKDF2-SHA256 (390000迭代)
- **微信**: wechatpy-enterprise
- **文件处理**: openpyxl + pandas

## 目录结构

```
kmgroup/
├── main.py                    # FastAPI 入口
├── models.py                  # SQLAlchemy ORM 模型
├── database.py                # 数据库连接配置
├── security.py                # 密码哈希 & 会话签名
├── auth_session.py           # 会话管理
├── product_service.py        # 产品自动建档服务
├── seq_utils.py              # PO/序号标准化工具
├── import_utils.py            # Excel/CSV 导入工具
├── wechat_runtime.py         # 企业微信通知
├── routers/                   # API 路由模块
│   ├── __init__.py
│   ├── users.py               # 用户认证 & 管理
│   ├── orders.py              # 订单管理
│   ├── products.py            # 产品管理
│   ├── production.py          # 生产进度
│   ├── inventory.py           # 仓库库存
│   ├── shipments.py           # 出货记录
│   ├── report.py              # 生产报表
│   ├── search.py              # 进度查询
│   ├── schedule.py            # 订单排产（Gantt图）
│   ├── config.py              # 系统配置
│   └── wechat.py              # 企业微信交互
├── static/                    # 前端静态文件
│   ├── index.html             # 登录页
│   ├── orders.html            # 订单管理页
│   ├── schedule.html          # 订单排产页
│   ├── production.html        # 生产进度页
│   ├── inventory.html        # 库存管理页
│   ├── shipments.html         # 出货记录页
│   ├── report.html            # 报表汇总页
│   ├── products.html          # 产品管理页
│   ├── users.html             # 用户管理页
│   ├── search.html            # 进度查询页
│   ├── css/                   # 样式文件
│   ├── js/                    # 前端脚本
│   ├── models/                # 3D 模型文件
│   └── background/            # 背景图片
├── config/                    # 配置文件（运行时生成）
├── Dockerfile                 # Docker 镜像构建
├── docker-compose.yml        # Docker Compose 编排
├── requirements.txt          # Python 依赖
├── .env.example              # 环境变量示例
├── .gitignore                # Git 忽略规则
└── README.md                 # 项目说明
```

## 快速开始

### 环境要求

- Python 3.12+
- PostgreSQL 14+
- Docker & Docker Compose（可选）

### 方式一：Docker 部署（推荐）

```bash
# 1. 克隆项目
git clone https://github.com/whoiskb-cn/kmgroup.git
cd kmgroup

# 2. 配置环境变量
cp .env.example .env
# 编辑 .env 填入实际值

# 3. 启动服务
docker-compose up -d
```

访问 `http://localhost:2006`

### 方式二：本地开发

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置环境变量
cp .env.example .env
# 编辑 .env 填入实际值

# 3. 启动服务
python -m uvicorn app.main:app --host 0.0.0.0 --port 2006 --reload
```

访问 `http://localhost:2006`

### 环境变量说明

| 变量 | 说明 | 示例 |
|------|------|------|
| `DB_HOST` | 数据库地址 | `localhost` |
| `DB_PORT` | 数据库端口 | `5432` |
| `DB_NAME` | 数据库名 | `kmgroup_db` |
| `DB_USER` | 数据库用户 | `postgres` |
| `DB_PASSWORD` | 数据库密码 | `your_password` |
| `SECRET_KEY` | 会话签名密钥（必填） | `随机字符串` |
| `ADMIN_USERNAME` | 初始管理员用户名 | `admin` |
| `ADMIN_PASSWORD` | 初始管理员密码 | `admin123` |
| `APP_PORT` | 服务端口 | `2006` |
| `APP_DEBUG` | 调试模式 | `false` |
| `ENABLE_SCHEDULER` | 定时任务 | `true` |

### 微信企业版配置

| 变量 | 说明 |
|------|------|
| `WECHAT_TOKEN` | 企业微信回调 Token |
| `WECHAT_ENCODING_AES_KEY` | 企业微信 AES 密钥 |
| `WECHAT_CORP_ID` | 企业 ID |
| `WECHAT_SECRET` | 应用 Secret |
| `WECHAT_AGENT_ID` | 应用 AgentId |
| `WECHAT_ADMIN_USER_IDS` | 管理员用户ID（多个用逗号分隔） |
| `WECHAT_NORMAL_USER_IDS` | 普通用户ID（多个用逗号分隔） |

## API 文档

启动服务后访问：

- Swagger UI: `http://localhost:2006/api/docs`
- ReDoc: `http://localhost:2006/api/redoc`

## 微信命令

管理员可用：
- `库存 图号` - 查询库存
- `进度 图号` - 查询生产进度
- `订单 图号` - 查询订单
- `入库 图号 数量` - 产品入库
- `待电镀 图号 数量` - 待电镀入库
- `寄电镀 图号 数量` - 寄电镀出库
- `出货 图号 PO 序号 数量` - 产品出货
- `报表 机床 图号 PO 序号 数量 时间 工序` - 上报生产记录
- `修改 图号 数量` - 库存修正

普通用户可用：库存查询、产品入库、待电镀入库、寄电镀出库、产品出货、报表上传。

## 数据模型

- **User** - 用户（角色：admin/operator/hongkong/mainland）
- **Product** - 产品（图号、8工序工时、3D模型路径）
- **Order** - 订单（关联产品、PO/序号、数量）
- **ProductionLog** - 生产日志
- **ProductionProcessState** - 工序完成状态
- **ProductionOrderState** - 订单完成状态
- **InventoryItem** - 库存（可出货/待电镀）
- **Shipment** - 出货记录
- **DailyReport** - 每日汇总

## License

MIT License

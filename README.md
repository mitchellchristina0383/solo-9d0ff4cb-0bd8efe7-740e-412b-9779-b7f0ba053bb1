# OCI Layer Merger / Rollout Planner

本服务提供两件互相独立的事：

1. **层合并**（`POST /merge`）：把两个连续的、**未压缩的** OCI 差异层合并成一个
   可复用的差异层。
2. **只读换版试算**（`POST /plan`）：在单节点解包盘余量有限、必须等新版预热完成
   才能停旧版的约束下，算出能否把一批服务全部换完，以及每轮的磁盘峰值。

`POST /plan` 不修改任何镜像、快照或文件，只做计算。

## 层合并

把两个连续的、**未压缩的** OCI 差异层（diff layer，tar 归档）合并成一个可复用的
差异层。合并层在任意下层（lower layer）上单独应用，与先应用第一个再应用第二个输入层
得到的路径、条目类型和文件内容完全一致。

## 支持的 tar 成员

| 成员 | 处理方式 |
| --- | --- |
| 普通文件 | 内容落盘到请求级临时目录，原样保留 mode/uid/gid/mtime 等元数据 |
| 目录 | 保留元数据；在"文件↔目录互换"、"删除后重建"时自动加 opaque 标记 |
| whiteout（`.wh.<name>`） | 删除同名子树；若在合并结果中仍需遮蔽下层则保留为 whiteout |
| opaque 目录标记（`.wh..wh..opq`） | 清除该目录下来自更低层的内容，同层条目始终保留 |

符号链接、硬链接、设备节点、FIFO 等其他成员类型一律拒绝（HTTP 400）。压缩 tar
（gzip/bzip2/xz/zstd，按魔数识别）、绝对路径、含 `..` 的路径、反斜杠/NUL 字符、
非零长度 whiteout、截断或损坏的 tar 同样拒绝。

## 为什么能在"任意下层"上等价

合并器在内存里维护一棵带 `origin`（来自第一层还是第二层）与 `opaque` 标记的树：

1. 顺序应用两个输入层，whiteout 删除子树，opaque 仅清除更低 `origin` 的节点；
2. 当一个目录建在"曾经是文件/被 whiteout 的目录"的位置时，强制打 opaque 标记，
   保证下层目录里可能存在的子项在任何基础层上都被隐藏；
3. 输出时，仍需遮蔽下层的路径输出 `.wh.` whiteout；opaque 目录输出
   `.wh..wh..opq`；其余按确定性顺序（目录、标记、文件）写出。

因此合并层携带的遮蔽信息不依赖于某个具体下层的内容。

## 运行

### Docker Compose（推荐）

```bash
docker compose up --build
# 打开 http://localhost:8000/docs
#  - /merge：上传 lower 与 upper 两个 tar
#  - /plan ：填入 OCI 配置 JSON，点 Execute 得到换版顺序与每轮峰值
```

`docker-compose.yaml` 以只读根文件系统、tmpfs `/tmp`、非 root 用户和
`no-new-privileges` 运行，内置 `/health` 健康检查。

### 本地直接运行

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## API

- `GET /health` — 存活探针，返回 `{"status":"ok"}`
- `GET /docs` — Swagger UI，可直接上传/下载
- `POST /merge` — `multipart/form-data`，字段：
  - `lower`：第一个（较低的）未压缩 tar
  - `upper`：第二个（较高的）未压缩 tar

成功返回 `200 application/x-tar`（附件名 `merged-layer.tar`）；任一输入非法返回
`400 application/json`，不会产生可下载产物；超过 2 GiB 返回 `413`；缺字段返回
`422`。

## 换版试算 `POST /plan`

请求体为 JSON（`Content-Type: application/json`），可直接在 `/docs` 的
`POST /plan` 里点 “Try it out” 使用预置示例。

| 字段 | 说明 |
| --- | --- |
| `services` | 至多 **10** 个待换服务；每项 `{name, old, new}`，`old`/`new` 是 OCI 镜像配置。`name` 必须唯一。 |
| `services[].old/new.diff_ids` | 该镜像配置 `rootfs.diff_ids` 的**完整有序**列表。 |
| `resident_images` | 换版期间始终常驻的镜像配置（可选，默认空）。 |
| `snapshots` | 每个被引用快照的独占字节数：`{diff_ids_prefix, exclusive_bytes}`。 |
| `budget_bytes` | 解包盘预算（字节）。 |

`old`/`new`/常驻镜像里的每个 `diff_ids` 前缀都必须在 `snapshots` 中给出独占字节数，
否则返回 `400`（`missing_snapshot_usage`）。同一前缀重复给出不同数值返回 `400`
（`conflicting_snapshot_usage`）；重复且相同则无影响。多于 10 个服务、负数字节数或
未知字段由 schema 校验拒绝（`422`）。

### 磁盘模型

- 一张镜像的每个**完整 diff_ids 前缀**（`[d1]`、`[d1,d2]`……）对应一个解包快照。
- 快照按完整前缀字符串识别：两个镜像前缀完全相同才算共享，只计一次；同一 digest
  出现在不同位置是不同快照。
- 某组镜像的总占用 = 其快照前缀**并集**中各快照的 `exclusive_bytes` 之和。

### 换版模型

- 每轮只换一个服务；预热时新旧两版同时驻留，因此该轮峰值 = 当前常驻集合
  （常驻镜像 + 已换服务的新版 + 未换服务的旧版）**加上**本轮新版本引入的快照。
- 新版就绪、旧版退出后才回收快照；仅当某快照已无人引用（常驻镜像、任一已换服务的
  新版、任一未换服务的旧版都不再引用）才计入 `retired_bytes`。
- 规划器对至多 10 个服务的全部 `2**10` 个状态做精确子集 DP，最小化整个换版过程的
  最大磁盘占用（含换版前的初始状态），而不是只贪心地看单轮。

### 响应

```json
{
  "feasible": true,
  "reason": null,
  "budget_bytes": 2300000000,
  "initial_usage_bytes": 2020000000,
  "final_usage_bytes": 2020000000,
  "order": ["billing", "orders"],
  "rounds": [
    {
      "round": 1,
      "service": "billing",
      "pre_round_usage_bytes": 2020000000,
      "warmup_added_bytes": 70000000,
      "peak_bytes": 2090000000,
      "headroom_bytes": 210000000,
      "retired_bytes": 80000000,
      "post_retire_usage_bytes": 2010000000
    },
    {
      "round": 2,
      "service": "orders",
      "pre_round_usage_bytes": 2010000000,
      "warmup_added_bytes": 260000000,
      "peak_bytes": 2270000000,
      "headroom_bytes": 30000000,
      "retired_bytes": 250000000,
      "post_retire_usage_bytes": 2020000000
    }
  ],
  "max_usage_bytes": 2270000000,
  "minimum_max_usage_bytes": 2270000000,
  "shortfall_bytes": 0
}
```

这是 `/docs` 中预置示例的真实响应：常驻镜像的 `host`/`daemon` 快照始终占用；
先换 `billing` 只引入 70 MB 新快照，回收 80 MB 旧快照后再换 `orders`，两轮峰值
分别为 2.09 GB 与 2.27 GB，均在 2.3 GB 预算内。

- `order` 是能保持**每轮峰值**都不超过预算的顺序；`rounds[].peak_bytes` 给出每轮
  峰值，`headroom_bytes` 为正表示该轮余量。
- 不可行时仍返回 `200`，`feasible=false`，`order=[]`、`rounds=[]`，并给出
  `reason`（`initial_usage_exceeds_budget` 或 `no_order_fits_budget`）、
  理论最优 `minimum_max_usage_bytes` 与 `shortfall_bytes`，便于判断差多少盘。
- `max_usage_bytes` 是所选顺序全过程中（含初始状态）的最大占用。

## 测试

```bash
pip install -r requirements.txt pytest httpx
python -m pytest
```

测试套件包含：

**层合并**

- 一个**独立实现的参考叠加器**（`tests/helpers.py`，节点树结构与生产代码不同），
  在两个刻意构造的不同下层 `BASE_A` / `BASE_B` 上逐场景比较
  `base+lower+upper` 与 `base+merged` 的完整快照；
- whiteout 跨层保留、目录删除重建、文件/目录双向互换、opaque 同层语义、
  根 opaque、嵌套 whiteout 等场景；
- 压缩输入、截断 tar、危险路径、符号链接、非空 whiteout、垃圾数据等无效输入；
- 基于 FastAPI TestClient 的 HTTP 集成测试（状态码、Content-Type、下载内容）。

**换版试算**（`tests/test_rollout.py`）

- 用**独立的朴素模拟器**（按快照前缀集合做集合并集、暴力枚举全部排列）交叉验证
  子集 DP 的最优峰值与每轮峰值；含 3 组固定种子的随机化场景；
- 共享前缀只计一次、前缀按位置区分、常驻镜像始终占用、共享旧快照在仍有引用时
  不被回收；
- 顺序影响峰值（紧预算下必须选对顺序）、初始即超预算、零服务、10 个服务；
- 缺快照用量、重复用量冲突、重复服务名、超过 10 个服务、负预算等错误路径；
- HTTP 层：`/plan` 的成功响应、不可行响应、`400` 错误码与消息、`422` 校验、
  未知字段拒绝、OpenAPI 暴露。

针对已部署服务的端到端脚本（仅用标准库，可用 `python -I` 运行）：

```bash
python -I scripts/selftest.py --base-url http://127.0.0.1:8000
```

除层合并用例外，它还会用一份**独立实现的前缀并集 + 全排列搜索**核对 `/plan`：
共享快照只加一次、换版顺序对峰值的影响、紧预算不可行、常驻镜像、缺用量与冲突
用量报错、10 个服务上限，以及试算后服务仍健康（只读性）。

## 已知限制（OCI 单差异层模型的固有边界）

合并器只依据两个输入 tar 的可见信息决定 opaque 标记。考虑这种情形：输入层
只包含一个普通目录成员 `d/`（没有 whiteout、没有同层文件互换），而某个未知下层
在同一路径 `d` 上恰好是一个**普通文件**。由于输入层没有任何信号表明发生过类型
互换，合并层无法在不假设下层内容的前提下判断是否需要 opaque——这是单个 OCI
差异层表达能力的固有限制，任何只看这两个 tar 的合并器都面临同样的不可判定性。

所有**在输入层内可观察**的遮蔽需求都被完整处理并测试覆盖：

- 输入层用 whiteout 删除目录后重建 → 重建目录带 `.wh..wh..opq`；
- 输入层内文件↔目录互换 → 新目录带 opaque，隐藏下层目录可能存在的子项；
- 输入层 opaque 标记、嵌套 whiteout、根 opaque → 原样保留并在两层间正确合成。

其他限制：仅接受未压缩 tar（按魔数拒绝 gzip/bzip2/xz/zstd）；不支持符号链接、
硬链接、设备节点、FIFO 等非常规成员；单层上传上限 2 GiB。

换版试算的边界：

- 至多 10 个待换服务（精确子集 DP，状态数 `2**10`）；
- 假定每个快照的独占字节数由调用方准确提供，服务不探测真实磁盘；
- 假定同一服务的旧版在新版预热期内一直在线，且各轮之间可完全回收已无人引用的快照；
- 只做规划，不执行镜像拉取、解包或服务重启。

## 目录结构

```
app/
  merger.py   # 核心合并逻辑（无 Web 依赖，可单独复用）
  rollout.py  # 只读换版规划：快照前缀集合、峰值模型、子集 DP（无 Web 依赖）
  main.py     # FastAPI 路由、请求校验、错误处理
tests/        # pytest 单元与集成测试
scripts/      # 针对运行中服务的独立自测脚本
Dockerfile
docker-compose.yaml
requirements.txt
```

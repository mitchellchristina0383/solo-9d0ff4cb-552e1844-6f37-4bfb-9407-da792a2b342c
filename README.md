# 服务卷每日巡检台（纽约时间）

一个 Flask 小应用：按 `America/New_York` 当地时间每天检查一次服务卷的可用字节数，
支持网页设置巡检时刻、演练时钟，并正确处理纽约夏令时换时。

## 功能与规则

- 每日巡检一次，读取 `VOLUME_PATH`（默认 `/volume`）挂载点的可用字节数（`shutil.disk_usage`）。
- 网页可设置每日巡检时刻（HH:MM），展示当天**计划时间**与**实际可用字节数**；
  读盘失败时展示错误信息（异常类型与内容），不产生假数据。
- 夏令时：
  - **春季跳时（gap）**：不存在的当地时刻顺延到跳时后的第一个有效分钟
    （例：2026-03-08 02:30 → 实际 03:00 EDT，即 07:00Z）。
  - **秋季重复（fold）**：取第一次出现（fold=0，EDT 那次）
    （例：2026-11-01 01:30 → 05:30Z，而非 06:30Z）。
- 读盘在调度锁之外执行：**读盘期间仍可修改时刻**。
  - 已开始的巡检保留原计划时间，读盘完成后按原计划落结果；
  - 尚未开始的计划立即按新时刻重排。
- 同一纽约当地日期只保留一份结果（`results.local_date` 主键，`INSERT OR REPLACE`）。
- 重启后：当天已到计划时间但没有结果的巡检会补做；
  重启前已处于 `running` 的中断巡检沿用**原计划时间（及原开始时间）**重新读盘。
- 网页可**设定**演练时钟到任意 UTC 时刻，也可按秒**推进**，并可恢复真实时间；
  时钟偏移持久化在 SQLite 中。

## 运行

### Docker Compose（交付方式）

```bash
docker compose up --build
# 打开 http://localhost:8000
```

- 数据库存放在命名卷 `inspector-data`（容器内 `/data/inspector.db`）。
- 宿主机根目录以只读方式挂为 `/volume` 作为被巡检的“服务卷”，可按需换成真实卷。
- `READ_DELAY_SECONDS` 给读盘增加固定延迟，方便演练“读盘期间改时刻”；生产可设 0。

### 本地直接运行（无 Docker 时）

```bash
python3 -m venv .venv --without-pip   # 若系统无 ensurepip，先用 get-pip.py 引导 pip
.venv/bin/python -m pip install -r requirements.txt
DB_PATH=./data/inspector.db VOLUME_PATH=./data .venv/bin/python wsgi.py
```

## 网页入口

| 页面/接口 | 方法 | 说明 |
| --- | --- | --- |
| `/` | GET | 当天计划、当天结果、演练时钟、最近记录 |
| `/check-time` | POST | 设置每日巡检时刻（表单字段 `check_time`） |
| `/clock/set` | POST | 设定演练时钟（ISO UTC，如 `2026-03-08T13:00`） |
| `/clock/advance` | POST | 推进演练时钟（字段 `seconds`） |
| `/clock/reset` | POST | 恢复真实时间 |

## 结构

```
inspector/
  timeutil.py    # 纽约时区、HH:MM 校验、gap/fold 排程
  storage.py     # SQLite：settings / plans / results
  scheduler.py   # 后台调度线程、演练时钟、重启补做、锁外读盘
  app.py         # Flask 应用工厂与路由
  templates/index.html
tests/           # unittest：时区 5 例、调度 11 例、网页 5 例（共 22 例）
wsgi.py          # 入口
```

## 测试

```bash
.venv/bin/python -m unittest discover -s tests -v
```

## 已验证的演练场景（真实 HTTP 端到端）

1. 换时：春季 02:30 顺延至 03:00 EDT（07:00Z）执行；秋季 01:30 取第一次 05:30Z 执行。
2. 读盘期间改时刻：8 秒读盘窗口内把 09:00 改为 11:00，进行中的计划与结果仍记 09:00，
   次日计划按 11:00 排。
3. 重启补做：
   - 读盘中杀进程，计划停在 `running` 且无结果，重启后沿用原计划/原开始时间补做成功；
   - 手工构造到期 `pending` 无结果计划，重启后自动补做，计划时间不变；
   - 当天已有结果时重启不重复读盘。
4. 读盘失败：挂载点不存在时网页显示错误徽标与异常文本，同日只记一份，计划状态 `error`。

## 已知限制 / 未完成项

- 交付环境没有 Docker 守护进程，`docker compose up` 未在本机实跑；
  已校验 compose YAML 语法与 Dockerfile，并用等价的本地 venv 完成全部功能与端到端验证。
- 内置 Flask 开发服务器为单进程多线程部署；若上多进程/多副本，
  需要额外的分布式锁（当前进程内 `RLock` 不跨进程），SQLite 也建议换成 C/S 数据库。
- 巡检时刻精度为分钟；轮询周期默认 1 秒，极端情况下实际触发比计划点晚 1 个轮询周期。

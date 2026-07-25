# Todo 管理系统

一个使用 Python 标准库实现的命令行待办事项管理系统，数据存储在 SQLite 中。

## 架构设计

```
┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│   app.py     │────▶│  storage.py  │────▶│  models.py   │
│  (CLI 界面)   │     │  (SQLite 层)  │     │  (数据模型)   │
└──────────────┘     └──────────────┘     └──────────────┘
                            │
                            ▼
                     ┌──────────────┐
                     │  todo.db     │
                     │  (SQLite DB) │
                     └──────────────┘
```

### 模块职责

| 模块 | 文件 | 职责 |
|------|------|------|
| **models** | `todo/models.py` | `Task` 数据类 — id (UUID)、title、completed、created_at |
| **storage** | `todo/storage.py` | `TaskStore` — SQLite CRUD 封装，自动建表、类型映射 |
| **app** | `todo/app.py` | CLI 入口 — argparse 子命令 + 错误处理 |
| **tests** | `tests/test_todo.py` | pytest 单元测试覆盖全部功能 |

### 设计决策

- **零外部依赖**：仅使用 Python 3.11+ 标准库（`dataclasses`、`sqlite3`、`argparse`、`uuid`）
- **TaskStore 连接复用**：`connect()` 幂等，自动建表；多次调用共享同一连接
- **异常体系**：`StorageError`(基类) → `NotFoundError`(未找到)
- **CLI 分离**：`main()` 返回 int 退出码，可通过 `argv` 参数测试，无需真实子进程
- **存储隔离**：每个 `TaskStore` 实例使用独立 DB 文件，测试用临时文件互不影响

## 快速开始

```bash
# 确保在项目根目录（包含 pyproject.toml）
pytest tests/test_todo.py -v     # 运行测试
```

## CLI 使用

所有命令接受可选的 `--db <path>` 参数，默认数据库为 `~/.todo.db`。

### 添加任务

```bash
python -m todo.app add "买牛奶"
python -m todo.app add "写周报"
```

### 列出任务

```bash
# 仅显示未完成的任务（默认）
python -m todo.app list

# 显示所有任务（包括已完成）
python -m todo.app list --all
```

### 标记完成

```bash
# id 来自 list 输出
python -m todo.app done <task-id>
```

### 删除任务

```bash
python -m todo.app delete <task-id>
```

### 查看帮助

```bash
python -m todo.app --help
python -m todo.app add --help
```

## 运行测试

```bash
# 全部测试
pytest tests/test_todo.py -v

# 仅测试某一模块
pytest tests/test_todo.py -v -k "TestTaskModel"
pytest tests/test_todo.py -v -k "TestTaskStore"
pytest tests/test_todo.py -v -k "TestCli"
```

## 示例会话

```bash
$ python -m todo.app add "Buy groceries"
Created task a1b2c3d4e5f6: Buy groceries

$ python -m todo.app add "Write report"
Created task f6e5d4c3b2a1: Write report

$ python -m todo.app list
  [ ] a1b2c3d4e5f6  Buy groceries
  [ ] f6e5d4c3b2a1  Write report

$ python -m todo.app done a1b2c3d4e5f6
Marked task a1b2c3d4e5f6 as done.

$ python -m todo.app list
  [ ] f6e5d4c3b2a1  Write report

$ python -m todo.app list --all
  [✓] a1b2c3d4e5f6  Buy groceries
  [ ] f6e5d4c3b2a1  Write report

$ python -m todo.app delete f6e5d4c3b2a1
Deleted task f6e5d4c3b2a1.
```

## 项目结构

```
todo/
├── __init__.py      # 包导出
├── models.py        # Task 数据模型
├── storage.py       # SQLite 存储层
├── app.py           # CLI 入口
└── README.md        # 本文档
tests/
└── test_todo.py     # 单元测试
```

# 语料标注与争议仲裁

项目使用 Python 标准库、SQLite 和 `http.server`，实现批次、指南版本、重复标注、分歧检测、仲裁、一致性指标、金标准冻结与导出，并以“提交前不可查看含答案讨论”的方式隔离讨论区答案。

## 启动

```bash
python app.py
```

默认地址 <http://127.0.0.1:8112>，默认数据库为 `corpus.db`。首次启动会写入两位标注员、一位仲裁员和一个含分歧的示例批次。

```bash
PORT=9002 CORPUS_DB=/tmp/corpus.db python app.py
```

## 测试

```bash
python -m unittest discover -s tests -v
```

测试包括：分配、标注、发现分歧、阻止提前冻结、仲裁、计算一致性、冻结和导出；另一条测试验证提交答案前后讨论可见性变化，以及错误角色不能领取标注任务。批量分派测试覆盖上限设置、本人/他人已领跳过、超额整体不写入、提交与退回后的名额重算、冻结批次禁止分派，以及对应 HTTP 接口。

## 接口

- `POST /api/users`、`POST /api/guidelines`、`POST /api/batches`
- `POST /api/batches/{id}/items`、`POST /api/batches/{id}/assign`
- `POST /api/batches/{id}/cap`、`POST /api/batches/{id}/bulk-assign`、`POST /api/batches/{id}/return`
- `POST /api/annotations`、`POST /api/adjudications`
- `GET /api/items/{id}?user_id=`
- `GET /api/batches/{id}/disagreements`
- `GET /api/batches/{id}/consistency`
- `POST /api/batches/{id}/freeze`
- `GET /api/batches/{id}/gold`

一致性同时返回逐条成对一致率和 Fleiss Kappa。冻结要求每条至少有两人标注、没有未仲裁分歧；冻结后不能修改标注，导出结果来自不可变的 `gold_records`。

## 批量分派

管理员按批次为每位标注员设置“未提交上限”，再一次提交标注员和条目序号批量分派：

- `POST /api/batches/{id}/cap`：`{annotator_id, cap}` 设置该标注员在本批次的未提交任务上限，返回上限与当前剩余名额。
- `POST /api/batches/{id}/bulk-assign`：`{annotator_id, ordinals:[...]}` 批量分派。已分给本人或他人的条目跳过并给出原因；若待新增条数超过剩余名额，则**整体不写入**（全或无）。返回 `assigned`、`skipped[].reason` 和 `remaining_quota`。
- `POST /api/batches/{id}/return`：`{annotator_id, ordinals:[...]}` 退回**未提交**任务，已提交的标注记录保持不变；退回后剩余名额重新计算。
- 名额只统计 `status='assigned'` 的分派：提交标注后占用释放，退回后重新占用；冻结批次不能设置上限、分派或退回。
- 代码按三层组织：判定规则在 `dispatch.py`，SQL 存储在 `database.py`，HTTP 与页面操作在 `app.py` / `static/index.html`。

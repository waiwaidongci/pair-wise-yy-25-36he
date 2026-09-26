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

测试包括：分配、标注、发现分歧、阻止提前冻结、仲裁、计算一致性、冻结和导出；另一条测试验证提交答案前后讨论可见性变化，以及错误角色不能领取标注任务。

## 接口

- `POST /api/users`、`POST /api/guidelines`、`POST /api/batches`
- `POST /api/batches/{id}/items`、`POST /api/batches/{id}/assign`
- `POST /api/batches/{id}/dispatch-cap`、`POST /api/batches/{id}/dispatch`、`POST /api/batches/{id}/release`
- `POST /api/annotations`、`POST /api/adjudications`
- `GET /api/items/{id}?user_id=`
- `GET /api/batches/{id}/disagreements`
- `GET /api/batches/{id}/consistency`
- `POST /api/batches/{id}/freeze`
- `GET /api/batches/{id}/gold`

一致性同时返回逐条成对一致率和 Fleiss Kappa。冻结要求每条至少有两人标注、没有未仲裁分歧；冻结后不能修改标注，导出结果来自不可变的 `gold_records`。

## 批量分派

管理员按批次为每位标注员设置“未提交上限”，再一次提交标注员和若干条目序号：

- `POST /api/batches/{id}/dispatch-cap`：请求体 `{"annotator_id":1,"cap":3}`，`cap=0` 取消上限；
- `POST /api/batches/{id}/dispatch`：请求体 `{"annotator_id":1,"ordinals":[3,4,5]}`，按提交顺序处理；
- `POST /api/batches/{id}/release`：请求体 `{"annotator_id":1,"ordinal":4}`，退回未提交任务。

分派返回 `assigned`（成功项）、`skipped`（含跳过原因：已分给本人 / 已分给他人 / 条目不在该批次中 / 请求内序号重复 / 序号非法 / 超出剩余名额本次未写入）和 `remaining`（剩余名额）。名额 = 上限 − 该标注员在本批次中状态为 `assigned` 的分派数；提交后不再占用名额，退回后名额重算，已提交标注保持不变。新增数超过剩余名额时整批不写入；冻结批次不能设置上限、分派或退回。判定、存储、页面操作分别位于 `dispatch.py`、`database.py`、`app.py` 与 `static/index.html`。

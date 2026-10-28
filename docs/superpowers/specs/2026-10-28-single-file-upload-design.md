# 单文件上传限制设计

## 目标

统一转换接口 `POST /api/v1/convert` 的 multipart `file` 字段最多出现一次。重复提交两个或更多 `file` 字段时明确拒绝，不创建转换任务。

## 接口契约

- 保留现有 `file: UploadFile` 单文件参数和 OpenAPI 契约。
- 在转换逻辑执行前读取已解析 multipart 表单中的全部 `file` 值。
- `len(form.getlist('file')) > 1` 时返回 HTTP 400：`{"detail":"file 只能上传一个文件"}`。
- 零个或一个 `file` 字段沿用现有 `file` / `url` 二选一规则。
- Excel、Word、图片、HTML、URL、HTML 源码、同步和异步行为均不变。

## 实现

在 `app/main.py` 的统一接口中注入 FastAPI `Request`，通过 `await request.form()` 获取同一份已解析表单并检查 `file` 字段数量。保留现有标量 `file` 参数，不改为数组，不新增通用依赖或抽象。数量检查在任务目录创建和源文件落盘之前完成。

## 测试

先在 `tests/smoke.sh` 增加同一请求携带两个 `file` 字段的负向用例，期望 HTTP 400，并在旧实现上确认测试失败。实现后重建 Docker 服务，运行完整冒烟回归，确认新增用例和原有全部用例通过。

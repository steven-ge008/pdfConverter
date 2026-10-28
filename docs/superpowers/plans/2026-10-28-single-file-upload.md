# 单文件上传限制 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 拒绝统一转换接口中重复出现的 multipart `file` 字段，并保持现有转换行为不变。

**Architecture:** 保留 FastAPI 标量 `file: UploadFile` 参数，在 handler 内通过同一个 `Request` 的缓存表单调用 `getlist('file')` 检查原始字段数量。校验在任务目录创建前执行，重复文件直接返回 HTTP 400。

**Tech Stack:** FastAPI、Starlette multipart、Bash curl 冒烟测试、Docker Compose。

---

### Task 1: 添加重复文件失败测试

**Files:**
- Modify: `tests/smoke.sh:161-169`
- Test: `tests/smoke.sh`

- [ ] **Step 1: 写入失败测试**

在统一入口参数校验段、Excel 样本存在时增加：

```bash
c=$(code -X POST "${BASE}/api/v1/convert" "${AUTH[@]}" -F 'sourceType=excel' \
      -F "file=@${SAMPLE_XLSX}" -F "file=@${SAMPLE_XLSX}")
[ "$c" = "400" ] && ok "重复 file 字段返回 400" || bad "重复 file 字段返回 ${c}（应为 400）"
```

- [ ] **Step 2: 启动未修改的服务并验证 RED**

Run:

```bash
docker compose -f docker-compose.yml up -d
BASE=http://localhost:8000 API_KEY=your-secret-key \
  SAMPLE_XLSX=/Users/youwenge/www/excel2pdf/test1.xlsx \
  SAMPLE_DOCX=/tmp/word_test.docx SAMPLE_IMAGE=/tmp/image_test.png \
  bash tests/smoke.sh
```

Expected: 新增“重复 file 字段返回 400”用例 FAIL，实际响应不是 400；其余既有用例保持通过。

### Task 2: 实现单文件数量校验

**Files:**
- Modify: `app/main.py:22`
- Modify: `app/main.py:129-143`

- [ ] **Step 1: 注入 Request 并检查原始 file 字段数量**

将 FastAPI 导入改为：

```python
from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
```

在 `convert_api` 的 `background` 参数后加入 `request: Request`，并在函数体开头加入：

```python
    form = await request.form()
    if len(form.getlist('file')) > 1:
        raise HTTPException(400, 'file 只能上传一个文件')
```

- [ ] **Step 2: 检查语法**

Run: `python3 -m py_compile app/main.py`
Expected: exit 0，无输出。

### Task 3: 更新接口文档并完整回归

**Files:**
- Modify: `README.md:144-149`
- Modify: `README.md:497-525`
- Test: `tests/smoke.sh`

- [ ] **Step 1: 更新文档契约**

在 `file` 字段说明中明确“最多一个，重复字段返回 400”，在 400 错误码场景中增加“重复 file 字段”，将完整冒烟测试数量从 25 改为 26。

- [ ] **Step 2: 重建并启动服务**

Run: `API_KEYS=your-secret-key docker compose -f docker-compose.yml up -d --build`
Expected: 镜像构建成功，容器 `pdf-converter` 启动。

- [ ] **Step 3: 执行完整回归**

Run:

```bash
BASE=http://localhost:8000 API_KEY=your-secret-key \
  SAMPLE_XLSX=/Users/youwenge/www/excel2pdf/test1.xlsx \
  SAMPLE_DOCX=/tmp/word_test.docx SAMPLE_IMAGE=/tmp/image_test.png \
  bash tests/smoke.sh
```

Expected: `结果：26 通过，0 失败`，新增重复文件用例及全部既有用例通过。

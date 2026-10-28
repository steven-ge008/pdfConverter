# pdf-converter

Excel / Word / 图片 / HTML 转 PDF 服务。基于 FastAPI + LibreOffice + Pillow + 无头 Chromium，以 Docker 镜像交付，运行即对外提供 HTTP 接口。

- **Excel 转 PDF**：上传本地 `.xlsx` / `.xlsm`，或提供 Excel 网络地址
- **Word 转 PDF**：上传本地 `.docx` / `.doc`，或提供 Word 网络地址
- **图片转 PDF**：上传本地 JPG/JPEG、PNG、WebP，或提供图片网络地址
- **HTML 转 PDF**：提供网页 URL（执行 JavaScript 渲染），或直接提交 HTML 源码
- **两种获取结果方式**：`sync=true` 直接返回 PDF 文件流；默认异步返回 `job_id`，轮询后下载
- **安全**：API Key 鉴权、SSRF 防护（含 Chromium 子请求拦截）、上传/下载限长
- **可靠**：任务持久化（SQLite），容器重启不丢失；结果文件 TTL 自动清理

---

## 一、快速启动

### 1. 构建镜像

在 `pdf-converter` 目录执行，构建上下文为当前目录；转换内核 `excel2pdf.py` 已包含在该目录中。

```bash
cd pdf-converter
docker build -t pdf-converter:latest .
```

### 2. 运行容器

```bash
docker run -d --name converter \
  -p 8000:8000 \
  -e API_KEYS=your-secret-key \
  -v converter-data:/data \
  --shm-size=256m \
  --memory=4g --cpus=2 \
  --restart unless-stopped \
  pdf-converter:latest
```

启动后：

- 接口地址：`http://localhost:8000`
- 在线文档（Swagger）：`http://localhost:8000/docs`
- 健康检查：`curl http://localhost:8000/healthz`

### 3. 使用 docker-compose（可选）

```bash
cd pdf-converter
API_KEYS=your-secret-key docker compose -f docker-compose.yml up -d --build
```

### 4. 构建加速（可选，国内网络）

Dockerfile 提供以下构建参数（`--build-arg`）：

| 构建参数 | 默认值 | 说明 |
| --- | --- | --- |
| `APT_MIRROR` | `mirrors.tuna.tsinghua.edu.cn` | Debian apt 镜像源 |
| `PIP_INDEX_URL` | 清华 PyPI 镜像 | pip 安装源 |
| `PLAYWRIGHT_DOWNLOAD_HOST` | 空（官方源） | Chromium 下载源；npmmirror 缺少 arm64 headless-shell，Apple Silicon 构建请保持默认 |

示例：

```bash
docker build -f Dockerfile -t pdf-converter:latest \
  --build-arg APT_MIRROR=mirrors.aliyun.com .
```

---

## 二、启动参数（环境变量）

全部通过环境变量配置，均有默认值，仅 `API_KEYS` 生产环境必须显式设置。

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `HOST` | `0.0.0.0` | 服务监听地址 |
| `PORT` | `8000` | 服务监听端口（容器内端口，映射用 `-p 宿主端口:8000`） |
| `API_KEYS` | 空 | 鉴权 Key，逗号分隔可配多个（如 `key1,key2`）；**为空则关闭鉴权，仅限受信内网** |
| `DATA_DIR` | `/data` | 任务元数据与结果 PDF 存放目录，**生产环境必须挂载 volume 持久化** |
| `EXCEL_TIMEOUT` | `180` | Excel 转换超时（秒），超时强杀 LibreOffice 进程组 |
| `HTML_TIMEOUT` | `90` | HTML 渲染超时（秒） |
| `WORD_TIMEOUT` | `180` | Word 转换超时（秒），超时强杀 LibreOffice 进程组 |
| `IMAGE_TIMEOUT` | `30` | 图片转换超时（秒） |
| `EXCEL_WORKERS` | `2` | Excel 并发转换上限（LibreOffice CPU 密集，不宜过大） |
| `HTML_WORKERS` | `2` | HTML 并发渲染上限（Chromium 内存密集） |
| `WORD_WORKERS` | `2` | Word 并发转换上限，使用独立队列与 worker 池 |
| `IMAGE_WORKERS` | `2` | 图片并发转换上限，使用独立队列与 worker 池 |
| `DOWNLOAD_TIMEOUT` | `60` | URL 下载超时（秒） |
| `MAX_DOWNLOAD_BYTES` | `52428800`（50MB） | URL 下载大小上限（字节） |
| `MAX_REDIRECTS` | `3` | URL 下载允许的重定向跳数，每跳重新做 SSRF 校验 |
| `MAX_UPLOAD_BYTES` | `52428800`（50MB） | 上传文件大小上限（字节），超限返回 413 |
| `JOB_TTL` | `86400`（24 小时） | 任务与结果文件保留期（秒），到期自动清理 |
| `CLEANUP_INTERVAL` | `600` | 过期任务清理间隔（秒） |
| `CJK_FONT` | `Noto Sans CJK SC` | 中文字体，统一替换工作簿声明的中文字体，消除随机 fallback |
| `SOFFICE` | `/usr/bin/soffice` | LibreOffice 可执行文件路径 |
| `EXCEL2PDF_PY` | `/opt/excel2pdf/excel2pdf.py` | 复用的转换内核路径（镜像内只读） |

---

## 三、鉴权

请求头两种方式任选其一，Key 值为 `API_KEYS` 中配置的任意一个：

| 方式 | 请求头示例 |
| --- | --- |
| X-API-Key | `X-API-Key: your-secret-key` |
| Bearer | `Authorization: Bearer your-secret-key` |

- 两个头同时传时优先取 `X-API-Key`
- Key 缺失或错误返回 `401`
- 探针接口 `/healthz`、`/readyz` 无需鉴权

---

## 四、接口列表

| 接口 | 方法 | 说明 | 鉴权 |
| --- | --- | --- | --- |
| `/healthz` | GET | 存活探针 | 否 |
| `/readyz` | GET | 就绪探针（检查 soffice / Chromium / 数据目录） | 否 |
| `/api/v1/convert` | POST | 统一转换入口，由 `sourceType` 指定 Excel / Word / 图片 / HTML | 是 |
| `/api/v1/jobs/{job_id}` | GET | 查询异步任务状态 | 是 |
| `/api/v1/jobs/{job_id}/download` | GET | 下载任务结果 PDF | 是 |

转换接口通用规则：

- 请求统一使用 `multipart/form-data`，`sourceType` 必填
- `sync=true`：同步等待转换完成，直接返回 PDF 文件流（`Content-Type: application/pdf`）
- `sync=false`（默认）：立即返回 `job_id`，通过轮询 + 下载获取结果

---

## 五、接口详情

### 5.1 统一转换 — `POST /api/v1/convert`

请求统一为 `multipart/form-data`。`sourceType=excel` 时使用以下字段。

**Request 字段**

| 字段 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `sourceType` | string | 是 | - | 源文件类型：`excel` / `word` / `image` / `html` |
| `file` | File | 与 `url` 二选一 | - | 上传的 Excel 文件，仅支持 `.xlsx` / `.xlsm` |
| `url` | string | 与 `file` 二选一 | - | Excel 文件网络地址，仅 http/https，SSRF 校验 |
| `sync` | boolean | 否 | `false` | `true` 直接返回 PDF 文件流；`false` 返回 `job_id` 走异步 |
| `page_config` | string(JSON) | 否 | - | 按 sheet 名覆盖分页配置，见下方说明 |

`page_config` 为 JSON 字符串，key 为 sheet 名，value 字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `fit_width` | int | 缩放至 N 页宽（通常设为 1，避免横向切断） |
| `fit_height` | int | 缩放至 N 页高；`0` 表示不限制，按内容自然分页 |
| `margins` | object | 页边距覆盖（英寸），可含 `left` / `right` / `top` / `bottom`，只覆盖给出的方向 |

**Response（异步，`sync=false`）**

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `job_id` | string | 任务 ID，用于查询与下载 |
| `status` | string | 固定为 `pending`，表示已入队 |
| `poll_url` | string | 任务查询接口路径 |

**Response（同步，`sync=true`）**

直接返回 PDF 二进制流，响应头：

| 响应头 | 说明 |
| --- | --- |
| `Content-Type` | `application/pdf` |
| `Content-Disposition` | `attachment; filename="{job_id}.pdf"` |

**Request / Response Demo**

异步（上传文件）：

```bash
curl -X POST http://localhost:8000/api/v1/convert \
  -H 'X-API-Key: your-secret-key' \
  -F 'sourceType=excel' \
  -F 'file=@/path/to/test1.xlsx'
```

```json
{
  "job_id": "952d82968be7467aa9f5853ca9e5034c",
  "status": "pending",
  "poll_url": "/api/v1/jobs/952d82968be7467aa9f5853ca9e5034c"
}
```

同步（网络地址 + 自定义分页）：

```bash
curl -X POST http://localhost:8000/api/v1/convert \
  -H 'X-API-Key: your-secret-key' \
  -F 'sourceType=excel' \
  -F 'url=https://example.com/files/order.xlsx' \
  -F 'sync=true' \
  -F 'page_config={"合同":{"fit_width":1,"fit_height":0,"margins":{"right":0.96}}}' \
  -o result.pdf
```

响应为 PDF 文件流，保存为 `result.pdf`。

---

### 5.2 Word 请求

`sourceType=word`，通过 LibreOffice Writer 转换，仍调用 `POST /api/v1/convert`。

**Request 字段**

| 字段 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `sourceType` | string | 是 | - | 固定为 `word` |
| `file` | File | 与 `url` 二选一 | - | 上传的 Word 文件，支持 `.docx` / `.doc` |
| `url` | string | 与 `file` 二选一 | - | Word 文件网络地址，URL 路径须以 `.docx` 或 `.doc` 结尾，仅 http/https，SSRF 校验 |
| `sync` | boolean | 否 | `false` | `true` 直接返回 PDF 文件流；`false` 返回 `job_id` 走异步 |

**Response 字段**：与统一接口一致（异步返回 `job_id` / `status` / `poll_url`；同步返回 PDF 流）。

**Request / Response Demo**

同步上传：

```bash
curl -X POST http://localhost:8000/api/v1/convert \
  -H 'X-API-Key: your-secret-key' \
  -F 'sourceType=word' \
  -F 'file=@/path/to/document.docx' \
  -F 'sync=true' \
  -o result.pdf
```

异步提交网络地址：

```bash
curl -X POST http://localhost:8000/api/v1/convert \
  -H 'X-API-Key: your-secret-key' \
  -F 'sourceType=word' \
  -F 'url=https://example.com/files/document.doc'
```

```json
{
  "job_id": "4f8c2a1b3d4e5f6a7b8c9d0e1f2a3b4c",
  "status": "pending",
  "poll_url": "/api/v1/jobs/4f8c2a1b3d4e5f6a7b8c9d0e1f2a3b4c"
}
```

---

### 5.3 图片请求

`sourceType=image`，每个任务转换一张图片。输出单页 A4 PDF，根据图片方向自动选择横向或纵向，保持宽高比缩放居中，不裁剪；透明区域使用白色背景，EXIF 方向自动校正。

**Request 字段**

| 字段 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `sourceType` | string | 是 | - | 固定为 `image` |
| `file` | File | 与 `url` 二选一 | - | 上传图片，支持 `.jpg` / `.jpeg` / `.png` / `.webp`，同时校验真实图片格式 |
| `url` | string | 与 `file` 二选一 | - | 图片网络地址，URL 路径须以支持的扩展名结尾，仅 http/https，SSRF 校验 |
| `sync` | boolean | 否 | `false` | `true` 直接返回 PDF 文件流；`false` 返回 `job_id` 走异步 |

**Response 字段**：与统一接口一致（异步返回 `job_id` / `status` / `poll_url`；同步返回 PDF 流）。

**Request / Response Demo**

同步上传：

```bash
curl -X POST http://localhost:8000/api/v1/convert \
  -H 'X-API-Key: your-secret-key' \
  -F 'sourceType=image' \
  -F 'file=@/path/to/photo.jpg' \
  -F 'sync=true' \
  -o result.pdf
```

异步提交网络地址：

```bash
curl -X POST http://localhost:8000/api/v1/convert \
  -H 'X-API-Key: your-secret-key' \
  -F 'sourceType=image' \
  -F 'url=https://example.com/images/photo.webp'
```

```json
{
  "job_id": "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d",
  "status": "pending",
  "poll_url": "/api/v1/jobs/1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d"
}
```

---

### 5.4 HTML 请求

`sourceType=html`，同样使用 `multipart/form-data`。HTML 不支持文件上传，`url` 与 `html` 必须二选一。

**Request 字段**

| 字段 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `sourceType` | string | 是 | - | 固定为 `html` |
| `url` | string | 与 `html` 二选一 | - | 待渲染网页地址，仅 http/https；执行 JavaScript，子请求同样做 SSRF 拦截 |
| `html` | string | 与 `url` 二选一 | - | HTML 源码字符串，直接渲染 |
| `sync` | boolean | 否 | `false` | `true` 直接返回 PDF 文件流；`false` 返回 `job_id` 走异步 |
| `format` | string | 否 | `A4` | 纸张大小：`A4` / `Letter` / `Legal` / `A3` 等 |
| `landscape` | boolean | 否 | `false` | 是否横向 |
| `print_background` | boolean | 否 | `true` | 是否打印背景色与背景图 |

**Response 字段**：与统一接口一致（异步返回 `job_id` / `status` / `poll_url`；同步返回 PDF 流）。

**Request / Response Demo**

同步（HTML 源码）：

```bash
curl -X POST http://localhost:8000/api/v1/convert \
  -H 'X-API-Key: your-secret-key' \
  -F 'sourceType=html' \
  --form-string 'html=<html><body><h1>中文渲染测试</h1><p>Hello PDF</p></body></html>' \
  -F 'sync=true' \
  -o result.pdf
```

异步（网页 URL，横向 A4）：

```bash
curl -X POST http://localhost:8000/api/v1/convert \
  -H 'X-API-Key: your-secret-key' \
  -F 'sourceType=html' \
  -F 'url=https://example.com' \
  -F 'format=A4' \
  -F 'landscape=true' \
  -F 'print_background=true'
```

```json
{
  "job_id": "7c9e2f1a4b3d4e5f8a0b1c2d3e4f5a6b",
  "status": "pending",
  "poll_url": "/api/v1/jobs/7c9e2f1a4b3d4e5f8a0b1c2d3e4f5a6b"
}
```

---

### 5.5 查询任务 — `GET /api/v1/jobs/{job_id}`

**路径参数**

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `job_id` | string | 提交转换时返回的任务 ID |

**Response 字段**

| 字段 | 类型 | 出现条件 | 说明 |
| --- | --- | --- | --- |
| `job_id` | string | 总是 | 任务 ID |
| `kind` | string | 总是 | 任务类型：`excel`、`word`、`image` 或 `html` |
| `status` | string | 总是 | 任务状态，见下方枚举 |
| `created_at` | number | 总是 | 创建时间（Unix 时间戳，秒） |
| `updated_at` | number | 总是 | 最近状态更新时间（Unix 时间戳，秒） |
| `pages` | int | `status=done` | PDF 页数 |
| `download_url` | string | `status=done` | 结果下载接口路径 |
| `error` | string | `status=failed` | 失败原因 |

`status` 枚举：

| 值 | 说明 |
| --- | --- |
| `pending` | 已入队，等待 worker 处理 |
| `running` | 正在转换 |
| `done` | 转换完成，可下载 |
| `failed` | 转换失败，`error` 含原因 |

**Response Demo**

转换中：

```json
{
  "job_id": "952d82968be7467aa9f5853ca9e5034c",
  "kind": "excel",
  "status": "running",
  "created_at": 1793154807.1713421,
  "updated_at": 1793154807.5
}
```

已完成：

```json
{
  "job_id": "952d82968be7467aa9f5853ca9e5034c",
  "kind": "excel",
  "status": "done",
  "created_at": 1793154807.1713421,
  "updated_at": 1793154807.8140898,
  "pages": 4,
  "download_url": "/api/v1/jobs/952d82968be7467aa9f5853ca9e5034c/download"
}
```

失败：

```json
{
  "job_id": "7c9e2f1a4b3d4e5f8a0b1c2d3e4f5a6b",
  "kind": "html",
  "status": "failed",
  "created_at": 1793154800.0,
  "updated_at": 1793154802.1,
  "error": "目标地址属于内网/保留地址，已拒绝: 169.254.169.254"
}
```

---

### 5.6 下载结果 — `GET /api/v1/jobs/{job_id}/download`

任务 `status=done` 后可下载，返回 PDF 文件流。

```bash
curl -H 'X-API-Key: your-secret-key' \
  http://localhost:8000/api/v1/jobs/952d82968be7467aa9f5853ca9e5034c/download \
  -o result.pdf
```

| 响应头 | 说明 |
| --- | --- |
| `Content-Type` | `application/pdf` |
| `Content-Disposition` | `attachment; filename="{job_id}.pdf"` |

---

### 5.7 探针

```bash
curl http://localhost:8000/healthz
# {"status":"ok"}

curl http://localhost:8000/readyz
# {"status":"ok","soffice":true,"chromium":true,"data_dir_writable":true}
```

`readyz` 字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `status` | string | 固定 `ok` |
| `soffice` | boolean | LibreOffice 可执行文件是否存在 |
| `chromium` | boolean | Chromium 是否已完成预热（`false` 时 HTML 转换不可用，Excel 不受影响） |
| `data_dir_writable` | boolean | 数据目录是否存在可写 |

---

## 六、异步调用完整流程

```bash
KEY=your-secret-key
BASE=http://localhost:8000

# 1. 提交任务，取 job_id
JOB=$(curl -s -X POST "$BASE/api/v1/convert" \
  -H "X-API-Key: $KEY" -F 'sourceType=excel' -F 'file=@test1.xlsx' | python3 -c 'import sys,json;print(json.load(sys.stdin)["job_id"])')

# 2. 轮询直到 done / failed
while true; do
  ST=$(curl -s "$BASE/api/v1/jobs/$JOB" -H "X-API-Key: $KEY" | python3 -c 'import sys,json;print(json.load(sys.stdin)["status"])')
  echo "status: $ST"
  [ "$ST" = "done" ] && break
  [ "$ST" = "failed" ] && exit 1
  sleep 2
done

# 3. 下载结果
curl -s "$BASE/api/v1/jobs/$JOB/download" -H "X-API-Key: $KEY" -o result.pdf
```

---

## 七、错误码

| HTTP 状态码 | 场景 |
| --- | --- |
| `400` | 参数错误（sourceType 缺失或非法、输入未提供或同时提供、文件扩展名与 sourceType 不符、空文件、page_config 非法 JSON）；Excel/Word/图片 URL 下载失败或 SSRF 拦截 |
| `401` | API Key 缺失或错误 |
| `404` | 任务不存在或已过期清理 |
| `409` | 任务尚未完成就请求下载 |
| `410` | 任务完成但结果文件已不存在（被清理） |
| `413` | 上传文件超过 `MAX_UPLOAD_BYTES` |
| `422` | 转换失败（LibreOffice 超时/异常、Chromium 渲染失败）；HTML URL 非法或 SSRF 拦截 |

错误响应统一为 JSON：`{"detail": "错误描述"}`；异步任务的失败原因通过任务查询接口的 `error` 字段获取。

---

## 八、注意事项

- **单 worker 运行**：队列与 Chromium 实例在进程内，uvicorn 必须 `--workers 1`（镜像 CMD 已固定），不要自行加多 worker
- **必须挂载 `/data`**：否则容器重建后任务记录与结果 PDF 丢失
- **资源建议**：2C4G 起步，`--shm-size=256m`；HTML 并发高时优先调大内存而非 `HTML_WORKERS`
- **同步请求有超时**：Excel 默认 180s、HTML 默认 90s，大文件或慢网页建议用异步方式
- **SSRF 防护**：仅允许 http/https，拒绝内网/保留地址（含 `169.254.169.254` 云元数据地址）、重定向逐跳校验、Chromium 子请求拦截；公网服务务必配置 `API_KEYS`

---

## 九、验证

镜像构建并启动后，可运行冒烟测试（探针、鉴权、SSRF 负向、统一入口参数校验、HTML/Excel/Word/图片同步与异步全流程；提供全部样本时共 25 项）：

```bash
BASE=http://localhost:8000 API_KEY=your-secret-key \
  SAMPLE_XLSX=test1.xlsx SAMPLE_DOCX=test1.docx SAMPLE_IMAGE=test1.png \
  bash tests/smoke.sh
```

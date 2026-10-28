#!/usr/bin/env bash
# 端到端冒烟测试。容器启动后在项目目录执行：
#   API_KEY=change-me SAMPLE_XLSX=test1.xlsx SAMPLE_DOCX=test1.docx SAMPLE_IMAGE=test1.png bash tests/smoke.sh
set -u

BASE="${BASE:-http://localhost:8000}"
KEY="${API_KEY:-change-me}"
SAMPLE_XLSX="${SAMPLE_XLSX:-test1.xlsx}"
SAMPLE_DOCX="${SAMPLE_DOCX:-test1.docx}"
SAMPLE_IMAGE="${SAMPLE_IMAGE:-test1.png}"
AUTH=(-H "X-API-Key: ${KEY}")

pass=0; fail=0
ok()   { echo "  PASS  $1"; pass=$((pass+1)); }
bad()  { echo "  FAIL  $1"; fail=$((fail+1)); }
code() { curl -s -o /dev/null -w '%{http_code}' "$@"; }

echo "== 1. 探针 =="
[ "$(code "${BASE}/healthz")" = "200" ] && ok "healthz 200" || bad "healthz"
curl -s "${BASE}/readyz"; echo

echo "== 2. 鉴权 =="
[ "$(code -X POST "${BASE}/api/v1/convert" -F 'sourceType=html' --form-string 'html=<p>x</p>')" = "401" ] \
  && ok "无 Key 返回 401" || bad "无 Key 未拦截"
[ "$(code -X POST "${BASE}/api/v1/convert" -H 'X-API-Key: wrong' -F 'sourceType=html' --form-string 'html=<p>x</p>')" = "401" ] \
  && ok "错误 Key 返回 401" || bad "错误 Key 未拦截"

echo "== 3. SSRF 负向用例（应全部 400/422，且不得产生 PDF）=="
for u in "http://169.254.169.254/latest/meta-data/" "http://127.0.0.1:6379/" \
         "http://localhost/" "http://10.0.0.1/" "http://192.168.1.1/" \
         "http://[::1]/" "file:///etc/passwd"; do
  c=$(code -X POST "${BASE}/api/v1/convert" "${AUTH[@]}" \
        -F 'sourceType=html' -F "url=${u}" -F 'sync=true')
  { [ "$c" = "400" ] || [ "$c" = "422" ]; } && ok "已拒绝 ${u} (${c})" || bad "${u} 返回 ${c}（应拒绝）"
done

echo "== 4. HTML 同步转换 =="
c=$(curl -s -o /tmp/smoke_html.pdf -w '%{http_code}' -X POST "${BASE}/api/v1/convert" \
      "${AUTH[@]}" -F 'sourceType=html' \
      --form-string 'html=<html><body><h1>中文渲染测试</h1><p>Hello PDF</p></body></html>' -F 'sync=true')
if [ "$c" = "200" ] && head -c 4 /tmp/smoke_html.pdf | grep -q '%PDF'; then
  ok "HTML 同步返回 PDF（$(stat -f%z /tmp/smoke_html.pdf 2>/dev/null || stat -c%s /tmp/smoke_html.pdf) 字节）"
else
  bad "HTML 同步转换失败 (${c})"
fi

echo "== 5. Excel 同步转换 =="
if [ -f "${SAMPLE_XLSX}" ]; then
  c=$(curl -s -o /tmp/smoke_excel.pdf -w '%{http_code}' -X POST "${BASE}/api/v1/convert" \
        "${AUTH[@]}" -F 'sourceType=excel' -F "file=@${SAMPLE_XLSX}" -F "sync=true")
  if [ "$c" = "200" ] && head -c 4 /tmp/smoke_excel.pdf | grep -q '%PDF'; then
    ok "Excel 同步返回 PDF"
  else
    bad "Excel 同步转换失败 (${c})"
  fi
else
  echo "  SKIP  未找到样本 ${SAMPLE_XLSX}"
fi

echo "== 6. Excel 异步任务（提交 → 轮询 → 下载）=="
if [ -f "${SAMPLE_XLSX}" ]; then
  resp=$(curl -s -X POST "${BASE}/api/v1/convert" "${AUTH[@]}" -F 'sourceType=excel' -F "file=@${SAMPLE_XLSX}")
  job=$(echo "${resp}" | sed -n 's/.*"job_id":"\([a-f0-9]*\)".*/\1/p')
  if [ -z "${job}" ]; then
    bad "未取得 job_id：${resp}"
  else
    ok "提交成功 job_id=${job}"
    st=""
    for _ in $(seq 1 60); do
      st=$(curl -s "${BASE}/api/v1/jobs/${job}" "${AUTH[@]}" | sed -n 's/.*"status":"\([a-z]*\)".*/\1/p')
      [ "${st}" = "done" ] && break
      [ "${st}" = "failed" ] && break
      sleep 2
    done
    if [ "${st}" = "done" ]; then
      ok "任务完成"
      c=$(curl -s -o /tmp/smoke_async.pdf -w '%{http_code}' "${BASE}/api/v1/jobs/${job}/download" "${AUTH[@]}")
      [ "$c" = "200" ] && ok "异步结果下载成功" || bad "下载失败 (${c})"
    else
      bad "任务终态异常：${st} $(curl -s "${BASE}/api/v1/jobs/${job}" "${AUTH[@]}")"
    fi
  fi
fi

echo "== 7. Word 同步转换 =="
if [ -f "${SAMPLE_DOCX}" ]; then
  c=$(curl -s -o /tmp/smoke_word.pdf -w '%{http_code}' -X POST "${BASE}/api/v1/convert" \
        "${AUTH[@]}" -F 'sourceType=word' -F "file=@${SAMPLE_DOCX}" -F "sync=true")
  if [ "$c" = "200" ] && head -c 4 /tmp/smoke_word.pdf | grep -q '%PDF'; then
    ok "Word 同步返回 PDF"
  else
    bad "Word 同步转换失败 (${c})"
  fi
else
  echo "  SKIP  未找到样本 ${SAMPLE_DOCX}"
fi

echo "== 8. Word 异步任务（提交 → 轮询 → 下载）=="
if [ -f "${SAMPLE_DOCX}" ]; then
  resp=$(curl -s -X POST "${BASE}/api/v1/convert" "${AUTH[@]}" -F 'sourceType=word' -F "file=@${SAMPLE_DOCX}")
  job=$(echo "${resp}" | sed -n 's/.*"job_id":"\([a-f0-9]*\)".*/\1/p')
  if [ -z "${job}" ]; then
    bad "未取得 Word job_id：${resp}"
  else
    ok "Word 提交成功 job_id=${job}"
    st=""
    for _ in $(seq 1 60); do
      st=$(curl -s "${BASE}/api/v1/jobs/${job}" "${AUTH[@]}" | sed -n 's/.*"status":"\([a-z]*\)".*/\1/p')
      [ "${st}" = "done" ] && break
      [ "${st}" = "failed" ] && break
      sleep 2
    done
    if [ "${st}" = "done" ]; then
      ok "Word 任务完成"
      c=$(curl -s -o /tmp/smoke_word_async.pdf -w '%{http_code}' "${BASE}/api/v1/jobs/${job}/download" "${AUTH[@]}")
      [ "$c" = "200" ] && ok "Word 异步结果下载成功" || bad "Word 下载失败 (${c})"
    else
      bad "Word 任务终态异常：${st} $(curl -s "${BASE}/api/v1/jobs/${job}" "${AUTH[@]}")"
    fi
  fi
fi

echo "== 9. 图片同步转换 =="
if [ -f "${SAMPLE_IMAGE}" ]; then
  c=$(curl -s -o /tmp/smoke_image.pdf -w '%{http_code}' -X POST "${BASE}/api/v1/convert" \
        "${AUTH[@]}" -F 'sourceType=image' -F "file=@${SAMPLE_IMAGE}" -F "sync=true")
  if [ "$c" = "200" ] && head -c 4 /tmp/smoke_image.pdf | grep -q '%PDF'; then
    ok "图片同步返回 PDF"
  else
    bad "图片同步转换失败 (${c})"
  fi
else
  echo "  SKIP  未找到样本 ${SAMPLE_IMAGE}"
fi

echo "== 10. 图片异步任务（提交 → 轮询 → 下载）=="
if [ -f "${SAMPLE_IMAGE}" ]; then
  resp=$(curl -s -X POST "${BASE}/api/v1/convert" "${AUTH[@]}" -F 'sourceType=image' -F "file=@${SAMPLE_IMAGE}")
  job=$(echo "${resp}" | sed -n 's/.*"job_id":"\([a-f0-9]*\)".*/\1/p')
  if [ -z "${job}" ]; then
    bad "未取得图片 job_id：${resp}"
  else
    ok "图片提交成功 job_id=${job}"
    st=""
    for _ in $(seq 1 60); do
      st=$(curl -s "${BASE}/api/v1/jobs/${job}" "${AUTH[@]}" | sed -n 's/.*"status":"\([a-z]*\)".*/\1/p')
      [ "${st}" = "done" ] && break
      [ "${st}" = "failed" ] && break
      sleep 2
    done
    if [ "${st}" = "done" ]; then
      ok "图片任务完成"
      c=$(curl -s -o /tmp/smoke_image_async.pdf -w '%{http_code}' "${BASE}/api/v1/jobs/${job}/download" "${AUTH[@]}")
      [ "$c" = "200" ] && ok "图片异步结果下载成功" || bad "图片下载失败 (${c})"
    else
      bad "图片任务终态异常：${st} $(curl -s "${BASE}/api/v1/jobs/${job}" "${AUTH[@]}")"
    fi
  fi
fi

echo "== 11. 统一入口参数校验 =="
c=$(code -X POST "${BASE}/api/v1/convert" "${AUTH[@]}" -F 'sourceType=pdf' --form-string 'html=<p>x</p>')
[ "$c" = "400" ] && ok "非法 sourceType 返回 400" || bad "非法 sourceType 返回 ${c}（应为 400）"
if [ -f "${SAMPLE_XLSX}" ]; then
  c=$(code -X POST "${BASE}/api/v1/convert" "${AUTH[@]}" -F 'sourceType=word' -F "file=@${SAMPLE_XLSX}")
  [ "$c" = "400" ] && ok "文件扩展名与 sourceType 不匹配返回 400" || bad "类型不匹配返回 ${c}（应为 400）"
else
  echo "  SKIP  未找到样本 ${SAMPLE_XLSX}"
fi

echo
echo "结果：${pass} 通过，${fail} 失败"
[ "${fail}" -eq 0 ]

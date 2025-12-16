# 🔐 验证码识别 HTTP 服务

基于 ddddocr 的轻量级在线验证码识别 HTTP 服务。提供简单的 JSON 接口用于提交 Base64 图片并返回识别结果；同时内置静态文档与在线测试页面（/docs）。

## ✨ 主要特性

- RESTful JSON 接口：POST /recognize（接受 base64 字符串）
- 内置交互式文档与测试页面：GET /docs
- 健康检查：GET /health
- 支持并发与 OCR 并发限制（环境变量配置）
- 支持 CORS（通过 ALLOWED_ORIGIN 配置）
- 日志友好，包含 request_id 便于排查

---

## 目录

- [快速开始](#快速开始)  
- [运行与配置](#运行与配置)  
- [API 说明](#api-说明)  
- [响应格式与错误处理](#响应格式与错误处理)  
- [静态 docs & 在线测试](#静态-docs--在线测试)  
- [依赖](#依赖)  
- [部署提示](#部署提示)  
- [许可](#许可)

---

## 🚀 快速开始

前提：Python 3.8+，pip

克隆仓库并进入目录：

```bash
git clone https://github.com/jayceecory-tech/captcha-http-service.git
cd captcha-http-service
```

安装依赖：

```bash
pip install -r requirements.txt
```

启动服务（默认监听 0.0.0.0:8080）：

```bash
# 使用默认端口 8080
python server.py

# 或指定端口，例如 9000
python server.py 9000
```

也可以通过环境变量设置端口（见下文配置节）。

访问示例：

- 根目录状态页: http://localhost:8080/
- 健康检查: http://localhost:8080/health
- 文档/测试: http://localhost:8080/docs
- API: POST http://localhost:8080/recognize

---

## 运行与配置（环境变量）

服务器支持通过环境变量做常用配置：

- PORT — 服务监听端口（默认 8080）  
- MAX_CONTENT_LENGTH — 最大允许的请求体/图片大小（字节，默认 10MB）  
- OCR_CONCURRENCY — OCR 并发数限制（默认 4）  
- ALLOWED_ORIGIN — CORS Allow-Origin 值（默认 "*"）  
- PREWARM_OCR — 启动时是否预热 OCR（"true"/"false"，默认 true）

示例：

```bash
export PORT=9000
export MAX_CONTENT_LENGTH=$((20 * 1024 * 1024))  # 20MB
export OCR_CONCURRENCY=2
export ALLOWED_ORIGIN="https://example.com"
export PREWARM_OCR=false
python server.py
```

日志会包含 request_id 字段，便于在并发环境中关联请求与日志条目。

---

## API 说明

注意：当前实现只接受 JSON POST 请求，body 必须包含 key 为 `base64` 的值（可包含或不包含 data:image/...;base64, 前缀）。

### POST /recognize

用途：识别由 Base64 编码的验证码图片，并返回识别结果。

请求示例（curl）：

```bash
curl -X POST http://localhost:8080/recognize \
  -H "Content-Type: application/json" \
  -d '{"base64":"data:image/png;base64,iVBORw0K..."}'
```

说明：

- body 必须是 JSON 字符串
- 字段名：`base64`，支持包含前缀（例如 `data:image/png;base64,`）或纯 base64 内容
- 服务会进行基本校验（长度、Base64 可解码、图片可识别），并在内部将图片转换为 RGB 后调用 ddddocr 进行识别
- OCR 并发受 OCR_CONCURRENCY 限制；超并发请求会按线程/信号量顺序排队

---

## 响应格式与错误处理

所有响应为 JSON。成功与错误格式如下（与当前 server.py 实现一致）。

成功示例（HTTP 200）：

```json
{
  "success": true,
  "code": 200,
  "message": "识别成功",
  "request_id": "f2a1c3...",
  "data": {
    "captcha": "AB12",
    "time_ms": 123.45,
    "length": 4
  }
}
```

- `captcha`：只包含字母与数字（服务器会过滤其他字符）
- `time_ms`：识别耗时（毫秒）
- `length`：识别结果字符串长度

错误示例（例如请求错误或解析失败）：

```json
{
  "success": false,
  "code": 400,
  "message": "缺少 base64 字段",
  "request_id": "f2a1c3...",
  "data": null
}
```

常见 HTTP/错误码：

- 200 — 成功
- 400 — 请求参数/JSON/图片无效（例如缺少 base64、Base64 解码失败、图片不可识别）
- 413 — 请求体过大（超出 MAX_CONTENT_LENGTH）
- 500 — 服务器内部错误（OCR 初始化失败、识别异常等）

---

## 静态 docs & 在线测试

仓库包含一个内置的交互式文档与在线测试页面：

- 访问 /docs 可看到 docs/index.html（页面允许粘贴或上传图片并直接向 /recognize 发送测试请求）
- 页面会自动将本地上传的图片转换为 data:...;base64 并填入请求体，便于快速调试

示例：打开 http://localhost:8080/docs 进行交互测试。

---

## 依赖

主要依赖已列在 requirements.txt：

- ddddocr >= 1.5.6
- Pillow >= 10.0.0

请确保在生产/部署环境中安装这些依赖。

---

## 部署提示

- Heroku / Railway 等平台：仓库包含 `Procfile`（web: python server.py）与 `runtime.txt`（python-3.12.0），可直接部署。
- Docker：仓库未包含 Dockerfile（如需我可帮加），但可以将本项目容器化并映射 8080 端口。
- 资源限制：在低内存或低 CPU 环境下，适当降低 OCR_CONCURRENCY 或禁用 PREWARM_OCR。

---

## 常见问题与调试

- 如果提示缺少依赖，请运行：`pip install -r requirements.txt`  
- 如果返回 `Base64 解码失败`：确认传入的 base64 字符串正确且未被截断；如果包含前缀，服务会自动处理。  
- 想在浏览器直接调用 /recognize：请设置 ALLOWED_ORIGIN 或在同域下访问 /docs 测试页面。

---

## LICENSE

本项目采用 Apache License 2.0（详见仓库 LICENSE 文件）。

---

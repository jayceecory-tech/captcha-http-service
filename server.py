#!/usr/bin/env python3
"""
验证码识别HTTP服务（带静态 docs 页面支持）
此版本在之前稳定实现的基础上，新增静态文档/交互页面路由：
- GET /docs          -> docs/index.html (交互式 API 文档 + 测试)
- GET /docs/...      -> 静态资源 (js/css/images) 位于 ./docs/ 下

其它功能保持不变：并发支持、OCR 并发限制、稳健 log_message、Base64 解码与 OCR 调用等。
"""
from __future__ import annotations

import io
import json
import os
import re
import sys
import time
import uuid
import base64
import binascii
import logging
import threading
from http import HTTPStatus
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, unquote
from pathlib import Path
from mimetypes import guess_type

try:
    import ddddocr
except Exception:
    ddddocr = None

from PIL import Image, UnidentifiedImageError

# ==================== 配置部分 ====================
DEFAULT_PORT = 8080
MAX_CONTENT_LENGTH = int(os.environ.get('MAX_CONTENT_LENGTH', 10 * 1024 * 1024))  # 10MB
OCR_CONCURRENCY = int(os.environ.get('OCR_CONCURRENCY', 4))
ALLOWED_ORIGIN = os.environ.get('ALLOWED_ORIGIN', '*')
PREWARM_OCR = os.environ.get('PREWARM_OCR', 'true').lower() in ('1', 'true', 'yes')

# Path to repository dir (assumes server.py sits in repo root)
BASE_DIR = Path(__file__).resolve().parent
DOCS_DIR = BASE_DIR / 'docs'

# ==================== Logging ====================
LOG_FORMAT = "%(asctime)s - %(levelname)s - %(request_id)s - %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, datefmt='%Y-%m-%d %H:%M:%S')
logger = logging.getLogger(__name__)

class RequestIdFilter(logging.Filter):
    def filter(self, record):
        if not hasattr(record, 'request_id'):
            record.request_id = 'n/a'
        return True

logging.getLogger().addFilter(RequestIdFilter())

# ==================== OCR 工具（单例 + 并发限制） ====================
_ocr_instance = None
_ocr_lock = threading.Lock()
_ocr_semaphore = threading.BoundedSemaphore(OCR_CONCURRENCY)

def get_ocr():
    global _ocr_instance
    with _ocr_lock:
        if _ocr_instance is None:
            if ddddocr is None:
                raise RuntimeError("ddddocr 未安装或导入失败")
            _ocr_instance = ddddocr.DdddOcr()
            logger.info("OCR 识别器初始化完成", extra={'request_id': 'startup'})
    return _ocr_instance

# ==================== Base64 / Image helpers ====================
def remove_base64_header(base64_str: str) -> str:
    if not isinstance(base64_str, str):
        return base64_str
    if ',' in base64_str:
        return base64_str.split(',', 1)[1]
    return base64_str

def validate_base64(base64_str: str) -> str:
    base64_str = base64_str.strip()
    pure = remove_base64_header(base64_str)
    padding = len(pure) % 4
    if padding:
        pure += '=' * (4 - padding)
    return pure

def decode_base64_to_image(pure_base64: str) -> Image.Image:
    if ddddocr is not None and hasattr(ddddocr, "base64_to_image"):
        try:
            img = ddddocr.base64_to_image(pure_base64)
            if isinstance(img, Image.Image):
                return img.convert("RGB")
        except Exception:
            logger.debug("ddddocr.base64_to_image failed; fallback to PIL", extra={'request_id': 'n/a'})

    try:
        img_bytes = base64.b64decode(pure_base64)
    except binascii.Error as e:
        raise ValueError("Base64 解码失败") from e

    if len(img_bytes) > MAX_CONTENT_LENGTH:
        raise ValueError("解码后图片过大")

    try:
        img = Image.open(io.BytesIO(img_bytes))
        return img.convert("RGB")
    except UnidentifiedImageError as e:
        raise ValueError("无法识别的图片格式") from e

# ==================== HTTP 处理器 ====================
class CaptchaHandler(BaseHTTPRequestHandler):
    """处理验证码识别请求"""

    def log_message(self, format: str, *args):
        request_id = getattr(self, 'request_id', 'n/a')
        client_ip = self.client_address[0] if getattr(self, 'client_address', None) else 'unknown'
        try:
            message = format % args if args else format
        except Exception:
            message = format
        logger.info("%s - %s", client_ip, message, extra={'request_id': request_id})

    def _send_cors_headers(self):
        origin = ALLOWED_ORIGIN
        if origin == '*':
            self.send_header('Access-Control-Allow-Origin', '*')
        else:
            self.send_header('Access-Control-Allow-Origin', origin)
            self.send_header('Access-Control-Allow-Credentials', 'true')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization')

    def _set_headers(self, status_code: int = 200, content_type: str = 'application/json'):
        self.send_response(status_code)
        self.send_header('Content-Type', content_type)
        self._send_cors_headers()
        self.end_headers()

    def _serve_static_file(self, rel_path: str):
        """
        Serve files from DOCS_DIR in a safe manner.
        rel_path: relative path under /docs (e.g., '' or 'index.html' or 'main.js')
        """
        # Normalize
        safe_rel = Path(unquote(rel_path)).resolve()
        # Prevent escaping DOCS_DIR
        try:
            # Build candidate path
            candidate = (DOCS_DIR / rel_path.lstrip('/')).resolve()
        except Exception:
            self._send_error_response(HTTPStatus.BAD_REQUEST, "无效的路径")
            return

        # Ensure candidate is inside DOCS_DIR
        try:
            candidate.relative_to(DOCS_DIR.resolve())
        except Exception:
            self._send_error_response(HTTPStatus.NOT_FOUND, "资源不存在")
            return

        if not candidate.exists() or not candidate.is_file():
            self._send_error_response(HTTPStatus.NOT_FOUND, "资源不存在")
            return

        mime, _ = guess_type(str(candidate))
        content_type = mime or 'application/octet-stream'
        try:
            with open(candidate, 'rb') as f:
                content = f.read()
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self._send_cors_headers()
            self.end_headers()
            self.wfile.write(content)
        except Exception:
            logger.exception("静态资源读取失败", extra={'request_id': getattr(self, 'request_id', 'n/a')})
            # If write fails, just ignore

    def do_OPTIONS(self):
        self._set_headers(200)

    def do_GET(self):
        self.request_id = uuid.uuid4().hex
        parsed = urlparse(self.path)
        path = parsed.path

        # Serve docs static site
        if path == '/docs' or path == '/docs/':
            # default to index.html
            self._serve_static_file('index.html')
            return
        if path.startswith('/docs/'):
            rel = path[len('/docs/'):]
            if rel == '':
                rel = 'index.html'
            self._serve_static_file(rel)
            return

        # Root status page
        if path == '/':
            self._set_headers(200, 'text/html')
            self.wfile.write(self._generate_status_page().encode('utf-8'))
            return

        # Health
        if path == '/health':
            self._set_headers(200)
            resp = {
                "status": "healthy",
                "service": "captcha-ocr",
                "timestamp": time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
            }
            self.wfile.write(json.dumps(resp).encode('utf-8'))
            return

        self._send_error_response(HTTPStatus.NOT_FOUND, "接口不存在")

    def do_POST(self):
        start_time = time.time()
        self.request_id = uuid.uuid4().hex

        try:
            if self.path != '/recognize':
                self._send_error_response(HTTPStatus.NOT_FOUND, "接口不存在，请使用 POST /recognize")
                return

            try:
                content_length = int(self.headers.get('Content-Length', 0))
            except Exception:
                content_length = 0

            if content_length == 0:
                self._send_error_response(HTTPStatus.BAD_REQUEST, "请求体为空")
                return
            if content_length > MAX_CONTENT_LENGTH:
                self._send_error_response(HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                                          f"请求体过大，最大支持 {MAX_CONTENT_LENGTH//1024//1024}MB")
                return

            post_data = self.rfile.read(content_length)
            try:
                data = json.loads(post_data.decode('utf-8'))
            except json.JSONDecodeError as e:
                self._send_error_response(HTTPStatus.BAD_REQUEST, f"JSON 格式错误: {str(e)}")
                return

            if 'base64' not in data:
                self._send_error_response(HTTPStatus.BAD_REQUEST, "缺少 base64 字段")
                return

            base64_str = data['base64']
            if not base64_str or len(base64_str) < 10:
                self._send_error_response(HTTPStatus.BAD_REQUEST, "base64 字符串无效或太短")
                return

            try:
                pure = validate_base64(base64_str)
                img = decode_base64_to_image(pure)
            except ValueError as e:
                self._send_error_response(HTTPStatus.BAD_REQUEST, str(e))
                return
            except Exception:
                logger.exception("解码图片时出错", extra={'request_id': self.request_id})
                self._send_error_response(HTTPStatus.INTERNAL_SERVER_ERROR, "图片解码失败")
                return

            try:
                ocr = get_ocr()
            except Exception:
                logger.exception("获取 OCR 实例失败", extra={'request_id': self.request_id})
                self._send_error_response(HTTPStatus.INTERNAL_SERVER_ERROR, "OCR 初始化失败")
                return

            with _ocr_semaphore:
                try:
                    # 尝试不同参数兼容 ddddocr 版本
                    try:
                        result = ocr.classification(img=img)
                    except TypeError:
                        buf = io.BytesIO()
                        img.save(buf, format='PNG')
                        img_bytes = buf.getvalue()
                        try:
                            result = ocr.classification(img_bytes=img_bytes)
                        except TypeError:
                            result = ocr.classification(img=img)
                except Exception as e:
                    logger.exception("OCR 识别异常", extra={'request_id': self.request_id})
                    self._send_error_response(HTTPStatus.INTERNAL_SERVER_ERROR, f"识别失败: {str(e)}")
                    return

            processing_time = (time.time() - start_time) * 1000.0
            result_text = ''.join(re.findall(r'[A-Za-z0-9]', str(result or '')))

            response = {
                "success": True,
                "code": 200,
                "message": "识别成功",
                "request_id": self.request_id,
                "data": {
                    "captcha": result_text,
                    "time_ms": round(processing_time, 2),
                    "length": len(result_text)
                }
            }
            self._set_headers(HTTPStatus.OK)
            self.wfile.write(json.dumps(response, ensure_ascii=False).encode('utf-8'))
            logger.info("识别成功: %s, 耗时: %.2fms", result_text, processing_time, extra={'request_id': self.request_id})
        except Exception:
            logger.exception("请求处理出错", extra={'request_id': getattr(self, 'request_id', 'n/a')})
            try:
                self._send_error_response(HTTPStatus.INTERNAL_SERVER_ERROR, "服务器内部错误")
            except Exception:
                pass

    def _generate_status_page(self) -> str:
        host, port = self.server.server_address
        display_host = host if host != '0.0.0.0' else 'localhost'
        server_url = f"http://{display_host}:{port}"
        return f"""<!doctype html>
<html>
<head><meta charset="utf-8"><title>验证码识别服务</title></head>
<body>
<h1>验证码识别HTTP服务</h1>
<p>服务运行中。请访问 <a href="/docs">/docs</a> 获取交互式 API 文档与在线测试页面。</p>
<p>本地访问: {server_url}</p>
</body>
</html>"""

    def _send_error_response(self, status_code: int | HTTPStatus, message: str):
        code = int(status_code.value if isinstance(status_code, HTTPStatus) else status_code)
        resp = {
            "success": False,
            "code": code,
            "message": message,
            "request_id": getattr(self, 'request_id', None),
            "data": None
        }
        try:
            self._set_headers(code)
            self.wfile.write(json.dumps(resp, ensure_ascii=False).encode('utf-8'))
        except Exception:
            pass
        logger.warning("请求错误 %s: %s", code, message, extra={'request_id': getattr(self, 'request_id', 'n/a')})

# ==================== 服务启动 ====================
def run_server(port: int | None = None):
    if port is None:
        port = int(os.environ.get('PORT', DEFAULT_PORT))
    server_address = ('0.0.0.0', port)
    httpd = ThreadingHTTPServer(server_address, CaptchaHandler)

    actual_port = httpd.server_address[1]
    logger.info("=" * 60, extra={'request_id': 'startup'})
    logger.info("🚀 验证码识别服务启动成功!", extra={'request_id': 'startup'})
    logger.info("📡 监听地址: http://0.0.0.0:%d", actual_port, extra={'request_id': 'startup'})
    logger.info("OCR 并发限制: %d", OCR_CONCURRENCY, extra={'request_id': 'startup'})
    logger.info("=" * 60, extra={'request_id': 'startup'})

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("收到停止信号，正在关闭服务...", extra={'request_id': 'startup'})
        httpd.server_close()
        logger.info("服务已安全停止", extra={'request_id': 'startup'})
    except Exception:
        logger.exception("服务异常停止", extra={'request_id': 'startup'})
        raise

# ==================== 主程序入口 ====================
if __name__ == '__main__':
    port = DEFAULT_PORT
    if len(sys.argv) > 1:
        try:
            p = int(sys.argv[1])
            if 1 <= p <= 65535:
                port = p
            else:
                print(f"⚠️ 端口号 {p} 无效，使用默认端口 {DEFAULT_PORT}")
        except ValueError:
            print(f"⚠️ 端口参数无效，使用默认端口 {DEFAULT_PORT}")

    try:
        import ddddocr as _check_ocr  # noqa: F401
        import PIL as _check_pil  # noqa: F401
        logger.info("✅ 所有依赖检查通过", extra={'request_id': 'startup'})
    except ImportError as e:
        print(f"❌ 缺少依赖: {e}")
        print("请运行: pip install -r requirements.txt")
        sys.exit(1)

    if PREWARM_OCR:
        def _prewarm():
            try:
                get_ocr()
            except Exception:
                logger.exception("OCR 预热失败（忽略）", extra={'request_id': 'startup'})
        t = threading.Thread(target=_prewarm, daemon=True)
        t.start()

    run_server(port)

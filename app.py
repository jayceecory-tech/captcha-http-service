"""
验证码识别 HTTP 服务 (Flask WSGI 版本)
专为 PythonAnywhere 等需要 WSGI 标准托管平台设计。
保留了原 server.py 的所有核心功能：OCR识别、静态docs页面、并发控制、完整日志。
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
from pathlib import Path
from functools import wraps
from mimetypes import guess_type

try:
    import ddddocr
except ImportError:
    ddddocr = None

from PIL import Image, UnidentifiedImageError
from flask import Flask, request, send_from_directory, jsonify, make_response

# ==================== 配置部分 ====================
# PythonAnywhere 会注入 PORT 环境变量，但我们用不到，Flask内部会处理。
# 这里配置的是应用本身的参数。
MAX_CONTENT_LENGTH = int(os.environ.get('MAX_CONTENT_LENGTH', 10 * 1024 * 1024))  # 10MB
OCR_CONCURRENCY = int(os.environ.get('OCR_CONCURRENCY', 4))
ALLOWED_ORIGIN = os.environ.get('ALLOWED_ORIGIN', '*')
PREWARM_OCR = os.environ.get('PREWARM_OCR', 'true').lower() in ('1', 'true', 'yes')

BASE_DIR = Path(__file__).resolve().parent
DOCS_DIR = BASE_DIR / 'docs'

# ==================== Flask 应用初始化 ====================
app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = MAX_CONTENT_LENGTH

# ==================== 日志配置 ====================
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
    """获取OCR实例（单例，线程安全）"""
    global _ocr_instance
    with _ocr_lock:
        if _ocr_instance is None:
            if ddddocr is None:
                raise RuntimeError("ddddocr 未安装或导入失败")
            _ocr_instance = ddddocr.DdddOcr()
            logger.info("OCR 识别器初始化完成", extra={'request_id': 'startup'})
    return _ocr_instance

# ==================== 辅助函数（与原 server.py 保持一致） ====================
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
    """解码Base64字符串为PIL Image对象，兼容多种方式"""
    # 优先使用 ddddocr 的高效方法
    if ddddocr is not None and hasattr(ddddocr, "base64_to_image"):
        try:
            img = ddddocr.base64_to_image(pure_base64)
            if isinstance(img, Image.Image):
                return img.convert("RGB")
        except Exception:
            logger.debug("ddddocr.base64_to_image 失败，回退至 PIL", extra={'request_id': 'n/a'})

    # 回退方案：标准 base64 + PIL
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

# ==================== Flask 路由与逻辑 ====================
def add_request_id():
    """为每个请求添加唯一ID的装饰器"""
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            request.request_id = uuid.uuid4().hex
            return f(*args, **kwargs)
        return decorated_function
    return decorator

def add_cors_headers(response):
    """为响应添加CORS头"""
    origin = ALLOWED_ORIGIN
    if origin == '*':
        response.headers['Access-Control-Allow-Origin'] = '*'
    else:
        response.headers['Access-Control-Allow-Origin'] = origin
        response.headers['Access-Control-Allow-Credentials'] = 'true'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    return response

@app.route('/docs/', defaults={'filename': 'index.html'})
@app.route('/docs/<path:filename>')
@add_request_id()
def serve_docs(filename):
    """提供 /docs/ 目录下的静态文件"""
    # 安全检查：确保请求的文件在 DOCS_DIR 目录内
    try:
        safe_path = (DOCS_DIR / filename).resolve()
        safe_path.relative_to(DOCS_DIR.resolve())
    except (ValueError, Exception):
        logger.warning(f"非法路径访问尝试: {filename}", extra={'request_id': request.request_id})
        return jsonify(success=False, code=404, message="资源不存在", request_id=request.request_id), 404

    if not (DOCS_DIR / filename).is_file():
        return jsonify(success=False, code=404, message="资源不存在", request_id=request.request_id), 404

    # 使用Flask的安全方法发送文件
    response = send_from_directory(DOCS_DIR, filename)
    return add_cors_headers(response)

@app.route('/')
@add_request_id()
def index():
    """根路径，返回状态页"""
    html_content = f"""
    <!doctype html>
    <html>
    <head><meta charset="utf-8"><title>验证码识别服务</title></head>
    <body>
    <h1>验证码识别HTTP服务 (Flask WSGI版)</h1>
    <p>服务运行正常。请访问 <a href="/docs/">/docs/</a> 获取交互式 API 文档与在线测试页面。</p>
    </body>
    </html>
    """
    response = make_response(html_content)
    response.headers['Content-Type'] = 'text/html'
    return add_cors_headers(response)

@app.route('/health')
@add_request_id()
def health_check():
    """健康检查端点"""
    resp = {
        "status": "healthy",
        "service": "captcha-ocr-flask",
        "timestamp": time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    }
    return add_cors_headers(jsonify(resp))

@app.route('/recognize', methods=['POST', 'OPTIONS'])
@add_request_id()
def recognize():
    """核心API：验证码识别"""
    start_time = time.time()

    # 处理 OPTIONS 预检请求
    if request.method == 'OPTIONS':
        response = make_response()
        response.status_code = 200
        return add_cors_headers(response)

    # 处理 POST 请求
    try:
        # 1. 获取并验证JSON数据
        if not request.is_json:
            return jsonify_error(400, "Content-Type 必须为 application/json")

        data = request.get_json()
        if not data or 'base64' not in data:
            return jsonify_error(400, "缺少 base64 字段")

        base64_str = data.get('base64', '')
        if not base64_str or len(base64_str) < 10:
            return jsonify_error(400, "base64 字符串无效或太短")

        # 2. 解码图片
        try:
            pure = validate_base64(base64_str)
            img = decode_base64_to_image(pure)
        except ValueError as e:
            return jsonify_error(400, str(e))
        except Exception:
            logger.exception("解码图片时出错", extra={'request_id': request.request_id})
            return jsonify_error(500, "图片解码失败")

        # 3. 获取OCR实例
        try:
            ocr = get_ocr()
        except Exception:
            logger.exception("获取 OCR 实例失败", extra={'request_id': request.request_id})
            return jsonify_error(500, "OCR 初始化失败")

        # 4. 进行识别（带并发限制）
        with _ocr_semaphore:
            try:
                # 兼容不同版本的 ddddocr 参数
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
                logger.exception("OCR 识别异常", extra={'request_id': request.request_id})
                return jsonify_error(500, f"识别失败: {str(e)}")

        # 5. 处理并返回结果
        processing_time = (time.time() - start_time) * 1000.0
        result_text = ''.join(re.findall(r'[A-Za-z0-9]', str(result or '')))

        response_data = {
            "success": True,
            "code": 200,
            "message": "识别成功",
            "request_id": request.request_id,
            "data": {
                "captcha": result_text,
                "time_ms": round(processing_time, 2),
                "length": len(result_text)
            }
        }

        logger.info(f"识别成功: {result_text}, 耗时: {processing_time:.2f}ms", extra={'request_id': request.request_id})
        return add_cors_headers(jsonify(response_data))

    except Exception:
        logger.exception("请求处理过程出现未预期错误", extra={'request_id': request.request_id})
        return jsonify_error(500, "服务器内部错误")

def jsonify_error(status_code: int, message: str):
    """统一的错误响应生成函数"""
    resp = {
        "success": False,
        "code": status_code,
        "message": message,
        "request_id": getattr(request, 'request_id', None),
        "data": None
    }
    logger.warning(f"请求错误 {status_code}: {message}", extra={'request_id': getattr(request, 'request_id', 'n/a')})
    return add_cors_headers(jsonify(resp)), status_code

# ==================== 应用启动与预热 ====================
if __name__ == '__main__':
    # 仅供本地测试，在PythonAnywhere上不会运行这部分
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=False)
else:
    # 当被WSGI服务器（如gunicorn、PythonAnywhere）导入时，执行预热
    # 这是PythonAnywhere等托管平台的标准模式
    logger.info("应用正在被WSGI服务器加载...", extra={'request_id': 'startup'})
    try:
        import ddddocr as _
        import PIL as _
        logger.info("✅ 所有依赖检查通过", extra={'request_id': 'startup'})
    except ImportError as e:
        logger.critical(f"❌ 缺少关键依赖: {e}", extra={'request_id': 'startup'})

    if PREWARM_OCR:
        def _prewarm_ocr():
            try:
                get_ocr()
                logger.info("✅ OCR 预热完成", extra={'request_id': 'startup'})
            except Exception:
                logger.exception("OCR 预热失败（忽略）", extra={'request_id': 'startup'})
        # 在后台线程中预热，避免阻塞WSGI加载
        t = threading.Thread(target=_prewarm_ocr, daemon=True)
        t.start()

# ==================== WSGI 入口点 ====================
# PythonAnywhere 等平台会寻找名为 `application` 的变量
application = app

#!/usr/bin/env python3
"""
验证码识别HTTP服务
启动命令: python server.py [端口号]
默认端口: 8080
"""

import json
import base64
import logging
import os
import time
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import ddddocr
from PIL import Image
import io
import re

# ==================== 配置部分 ====================
# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# 服务配置
DEFAULT_PORT = 8080
MAX_CONTENT_LENGTH = 10 * 1024 * 1024  # 10MB最大请求体

# ==================== OCR工具函数 ====================
# 全局OCR识别器（避免重复初始化）
_ocr_instance = None

def get_ocr():
    """获取OCR实例（单例模式）"""
    global _ocr_instance
    if _ocr_instance is None:
        try:
            _ocr_instance = ddddocr.DdddOcr()
            logger.info("✅ OCR识别器初始化完成")
        except Exception as e:
            logger.error(f"❌ OCR识别器初始化失败: {str(e)}")
            raise
    return _ocr_instance

def remove_base64_header(base64_str):
    """
    移除Base64数据头
    
    Args:
        base64_str: 可能包含data:image/png;base64,头的字符串
        
    Returns:
        纯净的Base64编码字符串
    """
    if not isinstance(base64_str, str):
        return base64_str
        
    # 查找第一个逗号并截断
    if ',' in base64_str:
        return base64_str.split(',', 1)[1]
    return base64_str

def validate_base64(base64_str):
    """
    验证并修正Base64字符串
    
    Args:
        base64_str: Base64字符串
        
    Returns:
        修正后的Base64字符串
    """
    # 移除可能的空白字符
    base64_str = base64_str.strip()
    
    # 移除数据头
    pure_base64 = remove_base64_header(base64_str)
    
    # 补全Base64填充
    missing_padding = 4 - len(pure_base64) % 4
    if missing_padding and missing_padding != 4:
        pure_base64 += '=' * missing_padding
        
    return pure_base64

# ==================== HTTP处理器 ====================
class CaptchaHandler(BaseHTTPRequestHandler):
    """处理验证码识别请求"""
    
    # 禁用默认的日志方法
    def log_message(self, format, *args):
        """自定义日志输出格式"""
        client_ip = self.client_address[0]
        logger.info(f"{client_ip} - {self.command} {self.path} - {format % args}")
    
    def _send_cors_headers(self):
        """发送CORS头"""
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization')
        self.send_header('Access-Control-Allow-Credentials', 'true')
    
    def _set_headers(self, status_code=200, content_type='application/json'):
        """设置HTTP响应头"""
        self.send_response(status_code)
        self.send_header('Content-Type', content_type)
        self._send_cors_headers()
        self.end_headers()
    
    def do_OPTIONS(self):
        """处理OPTIONS预检请求"""
        self._set_headers(200)
    
    def do_GET(self):
        """处理GET请求 - 返回服务状态"""
        parsed_path = urlparse(self.path)
        
        # 根路径返回状态页
        if parsed_path.path == '/':
            self._set_headers(200, 'text/html')
            html_content = self._generate_status_page()
            self.wfile.write(html_content.encode('utf-8'))
            return
        
        # 健康检查端点
        elif parsed_path.path == '/health':
            self._set_headers(200)
            response = {
                "status": "healthy",
                "service": "captcha-ocr",
                "timestamp": time.time()
            }
            self.wfile.write(json.dumps(response).encode('utf-8'))
            return
        
        # 其他GET请求返回404
        else:
            self._send_error_response(404, "接口不存在")
            return
    
    def do_POST(self):
        """处理POST请求"""
        start_time = time.time()
        
        try:
            # 只接受 /recognize 路径
            if self.path != '/recognize':
                self._send_error_response(404, "接口不存在，请使用 POST /recognize")
                return
            
            # 检查内容长度
            content_length = int(self.headers.get('Content-Length', 0))
            if content_length == 0:
                self._send_error_response(400, "请求体为空")
                return
            
            if content_length > MAX_CONTENT_LENGTH:
                self._send_error_response(413, f"请求体过大，最大支持 {MAX_CONTENT_LENGTH//1024//1024}MB")
                return
            
            # 读取请求体
            post_data = self.rfile.read(content_length)
            
            # 解析JSON
            try:
                data = json.loads(post_data.decode('utf-8'))
            except json.JSONDecodeError as e:
                self._send_error_response(400, f"JSON格式错误: {str(e)}")
                return
            
            # 检查必要字段
            if 'base64' not in data:
                self._send_error_response(400, "缺少base64字段")
                return
            
            base64_str = data['base64']
            
            if not base64_str or len(base64_str) < 10:
                self._send_error_response(400, "base64字符串无效或太短")
                return
            
            # 验证和处理Base64
            try:
                pure_base64 = validate_base64(base64_str)
                
                # 方法1：使用ddddocr的base64_to_image转换
                img = ddddocr.base64_to_image(pure_base64)
                
                # 获取OCR实例并识别
                ocr = get_ocr()
                
                # 根据版本兼容性调用
                try:
                    # 新版本支持img参数
                    result = ocr.classification(img=img)
                except TypeError as e:
                    # 降级方案：将PIL Image转换为bytes
                    img_byte_arr = io.BytesIO()
                    img.save(img_byte_arr, format='PNG')
                    img_bytes = img_byte_arr.getvalue()
                    
                    # 尝试不同参数名
                    try:
                        result = ocr.classification(img_bytes=img_bytes)
                    except TypeError:
                        result = ocr.classification(img=img_bytes)
                
                processing_time = (time.time() - start_time) * 1000  # 毫秒
                
                # 清理结果，只保留字母数字
                result = ''.join(re.findall(r'[A-Za-z0-9]', result))
                
                # 返回成功响应
                response = {
                    "success": True,
                    "code": 200,
                    "message": "识别成功",
                    "data": {
                        "captcha": result,
                        "time_ms": round(processing_time, 2),
                        "length": len(result)
                    }
                }
                
                self._set_headers(200)
                self.wfile.write(json.dumps(response, ensure_ascii=False).encode('utf-8'))
                
                logger.info(f"✅ 识别成功: {result}, 耗时: {processing_time:.2f}ms")
                
            except base64.binascii.Error:
                self._send_error_response(400, "Base64编码格式错误")
            except Exception as e:
                logger.error(f"识别过程出错: {str(e)}", exc_info=True)
                self._send_error_response(500, f"识别失败: {str(e)}")
                
        except Exception as e:
            logger.error(f"请求处理出错: {str(e)}", exc_info=True)
            self._send_error_response(500, "服务器内部错误")
    
    def _generate_status_page(self):
        """生成状态页面HTML"""
        return f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <title>验证码识别服务</title>
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <style>
                body {{
                    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                    line-height: 1.6;
                    max-width: 900px;
                    margin: 0 auto;
                    padding: 20px;
                    color: #333;
                }}
                .header {{
                    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                    color: white;
                    padding: 2rem;
                    border-radius: 10px;
                    margin-bottom: 2rem;
                }}
                .container {{
                    background: #f8f9fa;
                    padding: 2rem;
                    border-radius: 10px;
                    box-shadow: 0 2px 10px rgba(0,0,0,0.1);
                }}
                .endpoint {{
                    background: white;
                    padding: 1.5rem;
                    margin: 1rem 0;
                    border-radius: 8px;
                    border-left: 4px solid #4CAF50;
                }}
                pre {{
                    background: #2d2d2d;
                    color: #f8f8f2;
                    padding: 1rem;
                    border-radius: 5px;
                    overflow-x: auto;
                    font-size: 14px;
                }}
                code {{
                    background: #e9ecef;
                    padding: 2px 6px;
                    border-radius: 3px;
                    font-family: 'Courier New', monospace;
                }}
                .badge {{
                    display: inline-block;
                    padding: 3px 8px;
                    background: #4CAF50;
                    color: white;
                    border-radius: 12px;
                    font-size: 12px;
                    margin-right: 5px;
                }}
                .info-box {{
                    background: #e3f2fd;
                    border-left: 4px solid #2196F3;
                    padding: 1rem;
                    margin: 1rem 0;
                }}
            </style>
        </head>
        <body>
            <div class="header">
                <h1>🔐 验证码识别HTTP服务</h1>
                <p>基于 ddddocr 的在线验证码识别API服务</p>
                <p><strong>服务状态：</strong> <span style="color: #4CAF50;">● 运行正常</span></p>
            </div>
            
            <div class="container">
                <h2>📡 API端点</h2>
                
                <div class="endpoint">
                    <h3><span class="badge">POST</span> /recognize</h3>
                    <p>识别验证码图片</p>
                    
                    <h4>请求示例：</h4>
                    <pre>curl -X POST {self.get_server_url()}/recognize \\
  -H "Content-Type: application/json" \\
  -d '{{
    "base64": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUg..."
  }}'</pre>
                    
                    <div class="info-box">
                        <strong>📝 注意：</strong> Base64字符串可以包含 <code>data:image/png;base64,</code> 头部，程序会自动处理。
                    </div>
                </div>
                
                <div class="endpoint">
                    <h3><span class="badge">GET</span> /health</h3>
                    <p>健康检查接口</p>
                    <pre>curl {self.get_server_url()}/health</pre>
                </div>
                
                <h2>📋 请求/响应格式</h2>
                
                <h3>请求体 (JSON)：</h3>
                <pre>{{
  "base64": "字符串，验证码图片的Base64编码"
}}</pre>
                
                <h3>成功响应：</h3>
                <pre>{{
  "success": true,
  "code": 200,
  "message": "识别成功",
  "data": {{
    "captcha": "识别结果",
    "time_ms": 123.45,
    "length": 4
  }}
}}</pre>
                
                <h3>错误响应：</h3>
                <pre>{{
  "success": false,
  "code": 400,
  "message": "错误描述",
  "data": null
}}</pre>
                
                <h2>⚙️ 技术信息</h2>
                <ul>
                    <li><strong>Python版本：</strong> 3.12+</li>
                    <li><strong>核心库：</strong> ddddocr, Pillow</li>
                    <li><strong>最大图片：</strong> 10MB</li>
                    <li><strong>启动命令：</strong> <code>python server.py [端口]</code></li>
                </ul>
                
                <div class="info-box">
                    <strong>💡 提示：</strong> 本地运行时默认端口为8080，部署到云平台时会自动使用环境变量 <code>PORT</code>。
                </div>
            </div>
            
            <footer style="margin-top: 2rem; text-align: center; color: #666; font-size: 0.9rem;">
                <p>验证码识别服务 © {time.strftime('%Y')} | 服务启动时间: {time.strftime('%Y-%m-%d %H:%M:%S')}</p>
            </footer>
        </body>
        </html>
        """
    
    def get_server_url(self):
        """获取当前服务器URL"""
        host, port = self.server.server_address
        host = host if host != '0.0.0.0' else 'localhost'
        return f"http://{host}:{port}"
    
    def _send_error_response(self, status_code, message):
        """发送错误响应"""
        response = {
            "success": False,
            "code": status_code,
            "message": message,
            "data": None
        }
        
        self._set_headers(status_code)
        self.wfile.write(json.dumps(response, ensure_ascii=False).encode('utf-8'))
        
        logger.warning(f"⚠️  请求错误 {status_code}: {message}")

# ==================== 服务启动函数 ====================
def run_server(port=None):
    """启动HTTP服务器"""
    # 从环境变量或参数获取端口（云平台兼容）
    if port is None:
        port = int(os.environ.get('PORT', DEFAULT_PORT))
    
    server_address = ('0.0.0.0', port)  # 监听所有网络接口
    httpd = HTTPServer(server_address, CaptchaHandler)
    
    # 获取实际监听的地址
    actual_host = '0.0.0.0'
    actual_port = httpd.server_address[1]
    
    # 打印启动信息
    logger.info("=" * 60)
    logger.info(f"🚀 验证码识别服务启动成功!")
    logger.info(f"📡 监听地址: http://{actual_host}:{actual_port}")
    logger.info(f"🌐 本地访问: http://localhost:{actual_port}")
    logger.info(f"🔧 Python版本: {sys.version.split()[0]}")
    logger.info(f"⏰ 启动时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 60)
    logger.info("📚 可用接口:")
    logger.info(f"   GET  /           - 服务状态页面")
    logger.info(f"   GET  /health     - 健康检查")
    logger.info(f"   POST /recognize  - 验证码识别")
    logger.info("=" * 60)
    logger.info("🛑 按 Ctrl+C 停止服务")
    logger.info("")
    
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("")
        logger.info("🛑 收到停止信号，正在关闭服务...")
        httpd.server_close()
        logger.info("✅ 服务已安全停止")
    except Exception as e:
        logger.error(f"❌ 服务异常停止: {str(e)}")
        raise

# ==================== 主程序入口 ====================
if __name__ == '__main__':
    # 解析命令行参数
    port = DEFAULT_PORT
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
            if not (1 <= port <= 65535):
                print(f"⚠️  端口号 {port} 无效，使用默认端口 {DEFAULT_PORT}")
                port = DEFAULT_PORT
        except ValueError:
            print(f"⚠️  端口参数无效，使用默认端口 {DEFAULT_PORT}")
            port = DEFAULT_PORT
    
    # 检查依赖
    try:
        import ddddocr
        import PIL
        logger.info("✅ 所有依赖检查通过")
    except ImportError as e:
        print(f"❌ 缺少依赖: {e}")
        print("请运行: pip install -r requirements.txt")
        sys.exit(1)
    
    # 启动服务
    run_server(port)

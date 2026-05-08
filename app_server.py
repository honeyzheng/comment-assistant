"""
评论运营助手 - 线上部署版 (Flask)
部署到 Render 免费云平台，团队所有人通过浏览器访问即可使用
"""
import os
import sys
import json
import re
import gzip
import ssl
import logging
import urllib.request
import urllib.error
import random
from typing import List, Dict, Optional
from urllib.parse import urlparse
from flask import Flask, request, jsonify, send_file

# ============ Flask App ============
app = Flask(__name__, static_folder=None)
app.config['JSON_AS_ASCII'] = False

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

# ============ AI配置（环境变量优先）============
def get_ai_config():
    """获取AI配置，优先使用环境变量"""
    return {
        "api_base": os.environ.get("AI_API_BASE", "https://api.siliconflow.cn/v1"),
        "api_key": os.environ.get("AI_API_KEY", ""),
        "model": os.environ.get("AI_MODEL", "Qwen/Qwen2.5-72B-Instruct"),
    }


# ============ 文章抓取模块 ============
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

QQ_API_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://new.qq.com/",
}


def _make_request(url: str, headers: Dict = None, timeout: int = 15) -> bytes:
    if headers is None:
        headers = HEADERS.copy()
    if "Accept-Encoding" not in headers:
        headers["Accept-Encoding"] = "gzip, deflate"
    req = urllib.request.Request(url, headers=headers)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as response:
        raw_data = response.read()
        if raw_data[:2] == b'\x1f\x8b':
            raw_data = gzip.decompress(raw_data)
        return raw_data


def _decode_response(raw_data: bytes) -> str:
    for encoding in ["utf-8", "gbk", "gb2312", "latin1"]:
        try:
            return raw_data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw_data.decode("utf-8", errors="ignore")


def _clean_html(text: str) -> str:
    text = re.sub(r'<script[^>]*>[\s\S]*?</script>', '', text, flags=re.IGNORECASE)
    text = re.sub(r'<style[^>]*>[\s\S]*?</style>', '', text, flags=re.IGNORECASE)
    text = re.sub(r'<br\s*/?>|</p>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<[^>]+>', '', text)
    text = text.replace('&nbsp;', ' ')
    text = text.replace('&lt;', '<').replace('&gt;', '>')
    text = text.replace('&amp;', '&').replace('&quot;', '"').replace('&#39;', "'")
    text = re.sub(r'&#\d+;', '', text)
    text = re.sub(r'&\w+;', '', text)
    text = text.replace('\\n', '\n').replace('\\t', ' ').replace('\\/', '/')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def validate_url(url: str) -> bool:
    try:
        result = urlparse(url)
        return all([result.scheme in ('http', 'https'), result.netloc])
    except Exception:
        return False


def _fetch_via_jina(url: str, timeout: int = 20) -> Dict[str, str]:
    """使用 Jina Reader API 提取网页正文（免费、支持JS渲染页面）"""
    result = {"title": "", "content": ""}
    try:
        jina_url = f"https://r.jina.ai/{url}"
        headers = {
            "Accept": "text/plain",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "X-Return-Format": "text",
        }
        raw = _make_request(jina_url, headers=headers, timeout=timeout)
        text = raw.decode("utf-8", errors="ignore").strip()
        
        if text and len(text) > 50:
            # Jina 返回 markdown 格式，第一行通常是标题
            lines = text.split('\n')
            title_line = ""
            content_lines = []
            found_title = False
            
            for line in lines:
                stripped = line.strip()
                if not found_title and stripped.startswith('# '):
                    title_line = stripped[2:].strip()
                    found_title = True
                elif not found_title and stripped.startswith('Title:'):
                    title_line = stripped[6:].strip()
                    found_title = True
                elif stripped and not stripped.startswith('URL Source:') and not stripped.startswith('Markdown Content:'):
                    content_lines.append(stripped)
            
            if title_line:
                result["title"] = title_line
            
            content_text = '\n'.join(content_lines)
            # 去掉 markdown 图片标记等
            content_text = re.sub(r'!\[([^\]]*)\]\([^)]+\)', '', content_text)
            content_text = re.sub(r'\[([^\]]*)\]\([^)]+\)', r'\1', content_text)
            content_text = re.sub(r'\n{3,}', '\n\n', content_text).strip()
            
            if len(content_text) > 30:
                result["content"] = content_text
    except Exception as e:
        logger.info(f"Jina fetch failed for {url}: {e}")
    return result


def _fetch_via_readability_api(url: str, timeout: int = 15) -> Dict[str, str]:
    """备用：使用 12ft.io 风格的 HTML 直抓 + 加强解析"""
    result = {"title": "", "content": ""}
    try:
        # 用移动端 UA 有时能拿到更完整的内容（绕过一些JS渲染限制）
        mobile_headers = {
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Accept-Encoding": "gzip, deflate",
        }
        raw = _make_request(url, headers=mobile_headers, timeout=timeout)
        html = _decode_response(raw)
        
        # 标题
        og_match = re.search(r'<meta[^>]*property="og:title"[^>]*content="([^"]*)"', html)
        if og_match and og_match.group(1).strip():
            result["title"] = og_match.group(1).strip()
        if not result["title"]:
            title_match = re.search(r'<title[^>]*>(.*?)</title>', html, re.DOTALL)
            if title_match:
                result["title"] = _clean_html(title_match.group(1)).split('-')[0].strip()
        
        # 正文 - 先尝试 og:description（通常包含完整摘要）
        og_desc = re.search(r'<meta[^>]*property="og:description"[^>]*content="([^"]*)"', html)
        description = og_desc.group(1).strip() if og_desc else ""
        
        # 正文 - JSON-LD
        ld_matches = re.findall(r'<script[^>]*type="application/ld\+json"[^>]*>([\s\S]*?)</script>', html)
        for ld_text in ld_matches:
            try:
                ld_data = json.loads(ld_text)
                if isinstance(ld_data, list):
                    ld_data = ld_data[0] if ld_data else {}
                body = ld_data.get("articleBody") or ld_data.get("description") or ""
                if body and len(body) > 50:
                    result["content"] = body
                    return result
            except (json.JSONDecodeError, IndexError, AttributeError):
                continue
        
        # 正文 - 扩展容器选择器
        selectors = [
            r'<article[^>]*>([\s\S]*?)</article>',
            r'class="[^"]*article[-_]?content[^"]*"[^>]*>([\s\S]*?)</div>',
            r'class="[^"]*article[-_]?body[^"]*"[^>]*>([\s\S]*?)</div>',
            r'class="[^"]*post[-_]?content[^"]*"[^>]*>([\s\S]*?)</div>',
            r'class="[^"]*entry[-_]?content[^"]*"[^>]*>([\s\S]*?)</div>',
            r'class="[^"]*rich[-_]?text[^"]*"[^>]*>([\s\S]*?)</div>',
            r'class="[^"]*news[-_]?content[^"]*"[^>]*>([\s\S]*?)</div>',
            r'class="[^"]*detail[-_]?content[^"]*"[^>]*>([\s\S]*?)</div>',
            r'class="[^"]*main[-_]?content[^"]*"[^>]*>([\s\S]*?)</div>',
            r'id="[^"]*article[^"]*"[^>]*>([\s\S]*?)</div>',
            r'id="[^"]*content[^"]*"[^>]*>([\s\S]*?)</div>',
            r'itemprop="articleBody"[^>]*>([\s\S]*?)</div>',
            r'data-role="paragraph"[^>]*>([\s\S]*?)</div>',
        ]
        for pattern in selectors:
            match = re.search(pattern, html, re.DOTALL)
            if match:
                text = _clean_html(match.group(1))
                if len(text) > 80:
                    result["content"] = text
                    return result
        
        # 正文 - 所有 <p> 标签
        paragraphs = re.findall(r'<p[^>]*>([\s\S]*?)</p>', html, re.DOTALL)
        if paragraphs:
            texts = [_clean_html(p) for p in paragraphs if len(_clean_html(p)) > 15]
            if texts and sum(len(t) for t in texts) > 100:
                result["content"] = '\n'.join(texts)
                return result
        
        # 兜底：用 description
        if description and len(description) > 30:
            result["content"] = description
            
    except Exception as e:
        logger.info(f"Readability fetch failed: {e}")
    return result


def fetch_article(url: str, timeout: int = 15) -> Dict[str, str]:
    result = {"title": "", "content": "", "source": "", "url": url, "error": None}
    try:
        parsed = urlparse(url)
        domain = parsed.netloc.lower()

        # 识别来源
        if "qq.com" in domain:
            result["source"] = "腾讯新闻"
        elif "toutiao" in domain or "today" in domain:
            result["source"] = "今日头条"
        elif "weibo" in domain:
            result["source"] = "微博"
        elif "weixin" in domain or "mp.weixin" in domain:
            result["source"] = "微信公众号"
        elif "bilibili" in domain or "b23.tv" in domain:
            result["source"] = "B站"
        elif "douyin" in domain:
            result["source"] = "抖音"
        elif "163.com" in domain:
            result["source"] = "网易"
        elif "sina.com" in domain:
            result["source"] = "新浪"
        elif "sohu.com" in domain:
            result["source"] = "搜狐"
        elif "baidu" in domain:
            result["source"] = "百度"
        else:
            result["source"] = "网页"

        # === 策略1：先尝试平台专用API ===
        if "qq.com" in domain:
            result = _fetch_qq_news(url, result, timeout)
            if result["content"] and len(result["content"]) > 50:
                return result
        elif "weibo" in domain:
            result = _fetch_weibo(url, result, timeout)
            if result["content"] and len(result["content"]) > 50:
                return result

        # === 策略2：Jina Reader API（核心方案，支持JS渲染） ===
        if not result["content"] or len(result["content"]) < 50:
            logger.info(f"Using Jina Reader for: {url}")
            jina_result = _fetch_via_jina(url, timeout=25)
            if jina_result["content"] and len(jina_result["content"]) > 50:
                if not result["title"] and jina_result["title"]:
                    result["title"] = jina_result["title"]
                result["content"] = jina_result["content"]
                return result

        # === 策略3：移动端UA直抓+增强解析 ===
        if not result["content"] or len(result["content"]) < 50:
            logger.info(f"Using enhanced readability for: {url}")
            read_result = _fetch_via_readability_api(url, timeout)
            if read_result["content"] and len(read_result["content"]) > 50:
                if not result["title"] and read_result["title"]:
                    result["title"] = read_result["title"]
                result["content"] = read_result["content"]
                return result

        # === 策略4：旧的通用抓取作为最后兜底 ===
        if not result["content"] or len(result["content"]) < 50:
            result = _fetch_generic(url, result, timeout)

        # 过短的内容视为无效
        if result["content"] and len(result["content"]) < 30:
            result["content"] = ""

    except urllib.error.URLError as e:
        result["error"] = f"网络请求失败: {str(e.reason)}"
    except Exception as e:
        result["error"] = f"抓取失败: {str(e)}"
    return result


def _fetch_qq_news(url: str, result: Dict, timeout: int) -> Dict:
    article_id = ""
    match = re.search(r'/a/(\w+)', url)
    if match:
        article_id = match.group(1)
    result["source"] = "腾讯新闻"

    if article_id:
        # 尝试多个腾讯新闻API
        api_urls = [
            f"https://r.inews.qq.com/getSimpleNews?id={article_id}",
            f"https://i.news.qq.com/trpc.qqnews_web.kv_cache.KvCache/GetNewsContent?msg_id={article_id}",
            f"https://content.r.qq.com/getQQNewsNormalContent?id={article_id}",
        ]
        
        for api_url in api_urls:
            try:
                raw = _make_request(api_url, headers=QQ_API_HEADERS, timeout=timeout)
                text = raw.decode("utf-8")
                data = json.loads(text)
                
                # 提取标题
                title = data.get("title") or data.get("data", {}).get("title", "")
                if title and not result["title"]:
                    result["title"] = title
                
                # 提取正文 - 多种格式兼容
                content_text = ""
                
                # 格式1: content.text
                content_obj = data.get("content", {})
                if isinstance(content_obj, dict) and content_obj.get("text"):
                    content_text = content_obj["text"]
                
                # 格式2: data.content_html
                if not content_text:
                    content_text = data.get("data", {}).get("content_html", "")
                
                # 格式3: data.content
                if not content_text:
                    dc = data.get("data", {}).get("content", "")
                    if isinstance(dc, str) and len(dc) > 30:
                        content_text = dc
                
                # 格式4: newsData.content_list -> paragraphs
                if not content_text:
                    content_list = data.get("newsData", {}).get("content_list", [])
                    if not content_list:
                        content_list = data.get("data", {}).get("content_list", [])
                    if content_list:
                        parts = []
                        for item in content_list:
                            if isinstance(item, dict):
                                t = item.get("content", "") or item.get("text", "") or item.get("value", "")
                                if t and item.get("type", 0) in [0, 1, "text", "paragraph"]:
                                    parts.append(t)
                                elif t and len(t) > 10:
                                    parts.append(t)
                        if parts:
                            content_text = "\n".join(parts)
                
                if content_text:
                    cleaned = re.sub(r'<!--(VIDEO|IMG|AD|AIPOS)_\d+-->', '', content_text).strip()
                    if len(cleaned) > 20:
                        result["content"] = _clean_html(content_text)
                
                # 格式5: abstract 摘要作为备选
                if not result["content"] and data.get("abstract"):
                    result["content"] = data["abstract"]
                if not result["content"]:
                    abs_text = data.get("data", {}).get("abstract", "")
                    if abs_text:
                        result["content"] = abs_text
                
                if result["title"] and result["content"]:
                    return result
            except Exception:
                continue

    # 最后用通用方式抓取网页
    return _fetch_generic(url, result, timeout)


def _fetch_weibo(url: str, result: Dict, timeout: int) -> Dict:
    result["source"] = "微博"
    try:
        raw = _make_request(url, timeout=timeout)
        html = _decode_response(raw)
        render_match = re.search(r'\$render_data\s*=\s*(\[[\s\S]*?\])\[0\]', html)
        if render_match:
            try:
                data = json.loads(render_match.group(1))
                if isinstance(data, list) and len(data) > 0:
                    status = data[0].get("status", {})
                    if status.get("text"):
                        text = _clean_html(status["text"])
                        result["title"] = text[:60] + "..." if len(text) > 60 else text
                        result["content"] = text
            except json.JSONDecodeError:
                pass
        if not result["content"]:
            text_match = re.search(r'"text"\s*:\s*"((?:[^"\\]|\\.)*)"', html)
            if text_match:
                text = text_match.group(1).replace('\\"', '"').replace('\\n', '\n').replace('\\/', '/')
                text = _clean_html(text)
                result["title"] = text[:60] + "..." if len(text) > 60 else text
                result["content"] = text
    except Exception as e:
        result["error"] = f"抓取失败: {str(e)}"
    return result


def _fetch_generic(url: str, result: Dict, timeout: int) -> Dict:
    if not result.get("source"):
        result["source"] = "网页"
    try:
        raw = _make_request(url, timeout=timeout)
        html = _decode_response(raw)

        # 标题
        og_match = re.search(r'<meta[^>]*property="og:title"[^>]*content="([^"]*)"', html)
        if og_match and og_match.group(1).strip():
            result["title"] = og_match.group(1).strip()
        elif not result["title"]:
            h1_match = re.search(r'<h1[^>]*>(.*?)</h1>', html, re.DOTALL)
            if h1_match and len(_clean_html(h1_match.group(1))) > 2:
                result["title"] = _clean_html(h1_match.group(1))
            else:
                title_match = re.search(r'<title[^>]*>(.*?)</title>', html, re.DOTALL)
                if title_match:
                    t = _clean_html(title_match.group(1))
                    t = re.split(r'[-_|—]', t)[0].strip()
                    if len(t) > 2:
                        result["title"] = t

        # 正文 - JSON-LD
        if not result["content"]:
            ld_match = re.search(r'<script[^>]*type="application/ld\+json"[^>]*>([\s\S]*?)</script>', html)
            if ld_match:
                try:
                    ld_data = json.loads(ld_match.group(1))
                    if isinstance(ld_data, list):
                        ld_data = ld_data[0] if ld_data else {}
                    if ld_data.get("articleBody"):
                        result["content"] = ld_data["articleBody"]
                except (json.JSONDecodeError, IndexError):
                    pass

        # 正文 - 常见容器
        if not result["content"]:
            selectors = [
                r'<article[^>]*>([\s\S]*?)</article>',
                r'class="article-content[^"]*"[^>]*>([\s\S]*?)</div>',
                r'class="article-body[^"]*"[^>]*>([\s\S]*?)</div>',
                r'class="post-content[^"]*"[^>]*>([\s\S]*?)</div>',
                r'class="entry-content[^"]*"[^>]*>([\s\S]*?)</div>',
                r'class="content[^"]*"[^>]*>([\s\S]*?)</div>',
                r'itemprop="articleBody"[^>]*>([\s\S]*?)</div>',
            ]
            for pattern in selectors:
                match = re.search(pattern, html, re.DOTALL)
                if match:
                    text = _clean_html(match.group(1))
                    if len(text) > 80:
                        result["content"] = text
                        break

        # 正文 - <p>标签
        if not result["content"]:
            paragraphs = re.findall(r'<p[^>]*>(.*?)</p>', html, re.DOTALL)
            if paragraphs:
                texts = [_clean_html(p) for p in paragraphs if len(_clean_html(p)) > 15]
                if texts:
                    result["content"] = '\n'.join(texts)

        # 正文 - meta description
        if not result["content"]:
            desc_match = re.search(r'<meta[^>]*(?:name="description"|property="og:description")[^>]*content="([^"]*)"', html)
            if desc_match:
                result["content"] = desc_match.group(1)

    except Exception as e:
        if not result["title"] and not result["content"]:
            result["error"] = f"抓取失败: {str(e)}"
    return result


# ============ AI调用模块 ============
def call_ai(prompt: str, system_prompt: str = "", config: Dict = None) -> str:
    if config is None:
        config = get_ai_config()
    
    if not config.get("api_key"):
        raise ValueError("未配置AI API Key。请联系管理员设置。")

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    request_body = {
        "model": config["model"],
        "messages": messages,
        "temperature": 0.92,
        "max_tokens": 4096,
    }

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {config['api_key']}",
    }

    api_url = f"{config['api_base'].rstrip('/')}/chat/completions"

    try:
        data = json.dumps(request_body).encode("utf-8")
        req = urllib.request.Request(api_url, data=data, headers=headers, method="POST")
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(req, timeout=90, context=ctx) as response:
            result = json.loads(response.read().decode("utf-8"))
            if "choices" in result and len(result["choices"]) > 0:
                return result["choices"][0]["message"]["content"]
            else:
                raise ValueError("AI返回格式异常")
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise ValueError("API Key无效或已过期")
        elif e.code == 429:
            raise ValueError("请求过于频繁，请稍后重试")
        else:
            error_body = e.read().decode("utf-8", errors="ignore") if e.fp else ""
            raise ValueError(f"API请求失败(HTTP {e.code}): {error_body[:200]}")
    except urllib.error.URLError as e:
        raise ValueError(f"网络连接失败: {str(e.reason)}")
    except TimeoutError:
        raise ValueError("AI响应超时，请稍后重试")


def generate_comments(title: str, content: str, category: str = "通用",
                      count: int = 10, tone: str = "", requirements: str = "",
                      config: Dict = None) -> List[Dict]:
    content_preview = content[:800] if content else "（无正文）"

    system_prompt = """你是一个互联网评论模拟器。你的唯一任务是输出真实用户风格的评论。
规则：
- 每条评论像真人随手打的，有口语感、有情绪、有瑕疵
- 禁止出现：值得关注、让我们拭目以待、不禁令人深思、综上所述、作为一个XX
- 可以有：语气词(哈哈/啊/吧/嘛/草)、省略号、错别字、网络用语、表情
- 风格差异要大，像完全不同的人写的
- 严格按JSON格式输出，不要有任何多余解释"""

    user_prompt = f"""为这篇文章生成{count}条拟人评论：

标题：{title}
正文：{content_preview}
垂类：{category}
{f'基调偏好：{tone}' if tone else ''}
{f'特殊要求：{requirements}' if requirements else ''}

直接输出JSON数组，格式：
[{{"comment":"评论内容","persona":"人设标签","angle":"评论角度"}}]

人设从这些中选：大叔型、学生党、职场白领、热心大妈、段子手、技术宅、小镇青年、情感博主、杠精型、吃瓜群众、行业从业者、退休老人

只输出JSON，不要任何解释文字。"""

    raw_response = call_ai(user_prompt, system_prompt, config)
    comments = _parse_ai_response(raw_response)
    if not comments:
        raise ValueError("AI返回内容无法解析为评论，请重试")
    return comments


def _parse_ai_response(raw: str) -> List[Dict]:
    raw = raw.strip()
    
    # 提取代码块中的JSON
    if "```" in raw:
        json_match = re.search(r'```(?:json)?\s*\n?([\s\S]*?)\n?```', raw)
        if json_match:
            raw = json_match.group(1).strip()
    
    # 尝试解析JSON数组
    try:
        start = raw.find('[')
        end = raw.rfind(']')
        if start != -1 and end != -1 and end > start:
            json_str = raw[start:end+1]
            # 尝试修复常见JSON问题
            json_str = json_str.replace('\n', ' ')
            json_str = re.sub(r',\s*]', ']', json_str)  # 去除尾逗号
            json_str = re.sub(r',\s*}', '}', json_str)  # 去除对象尾逗号
            comments = json.loads(json_str)
            if isinstance(comments, list) and len(comments) > 0:
                valid = []
                for c in comments:
                    if isinstance(c, dict) and c.get("comment"):
                        valid.append({
                            "comment": c.get("comment", ""),
                            "persona": c.get("persona", "网友"),
                            "angle": c.get("angle", ""),
                        })
                if valid:
                    return valid
    except json.JSONDecodeError:
        pass
    
    # 备用方案：逐行匹配单个JSON对象
    try:
        objects = re.findall(r'\{[^{}]*"comment"[^{}]*\}', raw)
        if objects:
            valid = []
            for obj_str in objects:
                try:
                    c = json.loads(obj_str)
                    if c.get("comment"):
                        valid.append({
                            "comment": c.get("comment", ""),
                            "persona": c.get("persona", "网友"),
                            "angle": c.get("angle", ""),
                        })
                except json.JSONDecodeError:
                    continue
            if valid:
                return valid
    except Exception:
        pass
    
    # 最后兜底：把纯文本按行分割当评论
    lines = [l.strip() for l in raw.split('\n') if l.strip() and len(l.strip()) > 5]
    if lines:
        # 去掉可能的序号前缀
        valid = []
        for line in lines[:20]:
            cleaned = re.sub(r'^[\d]+[.、)\]：:]\s*', '', line)
            if len(cleaned) > 3 and not cleaned.startswith('{') and not cleaned.startswith('['):
                valid.append({
                    "comment": cleaned,
                    "persona": "网友",
                    "angle": "随机",
                })
        if valid:
            return valid
    
    return []


# ============ 页面路由 ============
@app.route('/')
def index():
    return send_file(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'index.html'))


# ============ API路由 ============
@app.route('/api/health', methods=['GET'])
def api_health():
    config = get_ai_config()
    return jsonify({
        "status": "ok",
        "ai_configured": bool(config.get("api_key")),
        "model": config.get("model", ""),
    })


@app.route('/api/fetch_article', methods=['POST'])
def api_fetch_article():
    data = request.get_json() or {}
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "请提供文章链接"}), 400
    if not validate_url(url):
        return jsonify({"error": "无效的URL格式"}), 400
    try:
        result = fetch_article(url)
        return jsonify(result)
    except Exception as e:
        logger.error(f"Fetch article error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/generate', methods=['POST'])
def api_generate():
    data = request.get_json() or {}
    url = data.get("url", "").strip()
    title = data.get("title", "").strip()
    content = data.get("content", "").strip()
    category = data.get("category", "通用")
    count = min(int(data.get("count", 10)), 20)
    tone = data.get("tone", "")
    requirements = data.get("requirements", "")

    # 如果有URL但没标题/正文，先抓取
    if url and not title and not content:
        try:
            if not validate_url(url):
                return jsonify({"error": "无效的URL"}), 400
            article = fetch_article(url)
            title = article.get("title", "")
            content = article.get("content", "")
        except Exception as e:
            return jsonify({"error": f"抓取失败: {e}"}), 500

    if not title and not content:
        return jsonify({"error": "无法获取文章内容，请手动输入标题"}), 400

    # 获取AI配置
    config = get_ai_config()
    
    # 允许前端传入自定义配置（团队成员各自的key）
    if data.get("api_key"):
        config = {
            "api_base": data.get("api_base", config["api_base"]),
            "api_key": data["api_key"],
            "model": data.get("model", config["model"]),
        }

    try:
        comments = generate_comments(
            title=title or "(未知标题)",
            content=content or title,
            category=category,
            count=count,
            tone=tone,
            requirements=requirements,
            config=config,
        )
        return jsonify({
            "comments": comments,
            "title": title,
            "count": len(comments),
        })
    except Exception as e:
        logger.error(f"Generate comments error: {e}")
        return jsonify({"error": str(e)}), 500


# ============ 启动 ============
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    host = os.environ.get('HOST', '0.0.0.0')
    logger.info(f"评论运营助手启动中... http://{host}:{port}")
    app.run(host=host, port=port, debug=False)

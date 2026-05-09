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
                # 视频文章：如果提取到的信息不够丰富（没有热评），尝试用Jina补充
                if result.get("is_video") and "热门评论" not in result["content"]:
                    try:
                        logger.info("Video article lacks comments, trying Jina Reader for extra context")
                        jina_result = _fetch_via_jina(url, timeout=20)
                        if jina_result["content"] and len(jina_result["content"]) > 80:
                            # 把 Jina 抓到的内容作为"页面文字内容"补充到后面
                            jina_text = jina_result["content"]
                            # 过滤掉和已有标题重复的部分
                            if result["title"] and result["title"] in jina_text:
                                jina_text = jina_text.replace(result["title"], "").strip()
                            if len(jina_text) > 50:
                                result["content"] += f"\n\n--- 页面补充内容 ---\n{jina_text[:600]}"
                    except Exception as e:
                        logger.info(f"Jina supplement for video failed: {e}")
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


def _extract_json_from_html(html: str, var_name: str) -> dict:
    """从HTML中提取JS变量赋值的JSON数据（用括号计数器精确匹配）"""
    marker = f"{var_name} = "
    start_idx = html.find(marker)
    if start_idx < 0:
        # 尝试不带空格的版本
        marker = f"{var_name}="
        start_idx = html.find(marker)
        if start_idx < 0:
            return {}
    
    json_start = start_idx + len(marker)
    if json_start >= len(html) or html[json_start] != '{':
        return {}
    
    # 用括号计数器找到完整JSON
    depth = 0
    i = json_start
    in_string = False
    escape_next = False
    
    while i < len(html):
        ch = html[i]
        if escape_next:
            escape_next = False
            i += 1
            continue
        if ch == '\\' and in_string:
            escape_next = True
            i += 1
            continue
        if ch == '"':
            in_string = not in_string
        if not in_string:
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    break
        i += 1
    
    if depth != 0:
        return {}
    
    raw_json = html[json_start:i+1]
    try:
        return json.loads(raw_json)
    except json.JSONDecodeError:
        return {}


def _fetch_qq_comments(article_id: str, cmt_id: str = "", timeout: int = 10) -> List[str]:
    """抓取腾讯新闻文章的热门评论（用于补充视频文章的语境信息）"""
    hot_comments = []
    try:
        # 方式1：coral评论API（需要cmt_id）
        if cmt_id:
            comment_url = f"http://coral.qq.com/article/{cmt_id}/comment?commentid=0&reqnum=20&tag=&callback=mainComment&_={random.randint(1000000000000,9999999999999)}"
            try:
                raw = _make_request(comment_url, timeout=timeout)
                text = raw.decode("utf-8", errors="ignore")
                # 去掉JSONP包裹
                json_text = re.sub(r'^mainComment\((.*)\);?$', r'\1', text.strip(), flags=re.DOTALL)
                data = json.loads(json_text)
                comment_list = data.get("data", {}).get("commentid", [])
                if comment_list:
                    for c in comment_list[:15]:
                        content = c.get("content", "").strip()
                        up = int(c.get("up", 0) or 0)
                        if content and len(content) > 3:
                            hot_comments.append({"content": content, "up": up})
            except Exception as e:
                logger.info(f"Coral comment API failed: {e}")
        
        # 方式2：新版评论API（用article_id直接查）
        if not hot_comments and article_id:
            comment_apis = [
                f"https://r.inews.qq.com/getQQNewsComment?article_id={article_id}&page=0&chlid=news_rss&qqnews_pgv_ref=aio",
                f"https://pacaio.match.qq.com/irs/rcd?cid={article_id}&token=&ext=top&page=0&expIds=",
            ]
            for api_url in comment_apis:
                try:
                    raw = _make_request(api_url, headers=QQ_API_HEADERS, timeout=timeout)
                    text = raw.decode("utf-8", errors="ignore")
                    data = json.loads(text)
                    if not isinstance(data, dict):
                        continue
                    # 不同API返回格式不同，安全获取
                    comments_data = None
                    data_inner = data.get("data", {})
                    if isinstance(data_inner, dict):
                        comments_data = data_inner.get("comments") or data_inner.get("comment_list")
                    if not comments_data:
                        comments_data = data.get("comments") or data.get("comment_list")
                    
                    if comments_data and isinstance(comments_data, list):
                        for c in comments_data[:15]:
                            if isinstance(c, dict):
                                content = c.get("content", "") or c.get("comment_content", "") or c.get("text", "")
                                up = int(c.get("up", 0) or c.get("agree_count", 0) or c.get("like_count", 0) or 0)
                                if content and isinstance(content, str) and len(content.strip()) > 3:
                                    hot_comments.append({"content": content.strip(), "up": up})
                    if hot_comments:
                        break
                except Exception:
                    continue
        
        # 按点赞数排序，取前10
        if hot_comments:
            hot_comments.sort(key=lambda x: x["up"], reverse=True)
            return [c["content"] for c in hot_comments[:10]]
    except Exception as e:
        logger.info(f"Fetch QQ comments failed: {e}")
    return hot_comments if isinstance(hot_comments, list) and all(isinstance(x, str) for x in hot_comments) else []


def _fetch_qq_news(url: str, result: Dict, timeout: int) -> Dict:
    """抓取腾讯新闻文章 - 通过解析页面中的 window.initData"""
    article_id = ""
    match = re.search(r'/a/(\w+)', url)
    if match:
        article_id = match.group(1)
    result["source"] = "腾讯新闻"

    # === 核心方案：直接抓取HTML解析 window.initData ===
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1",
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": "https://view.inews.qq.com/",
        }
        raw = _make_request(url, headers=headers, timeout=timeout)
        html = _decode_response(raw)
        
        # 提取 <title>
        title_match = re.search(r'<title[^>]*>(.*?)</title>', html, re.DOTALL)
        if title_match:
            title_text = _clean_html(title_match.group(1))
            # 去掉末尾的"-腾讯新闻"等
            title_text = re.sub(r'[-_|]\s*(腾讯新闻|腾讯网|新闻)$', '', title_text).strip()
            if title_text and not result["title"]:
                result["title"] = title_text
        
        # 提取 og:description（对视频文章特别有用，通常包含内容摘要）
        og_desc = ""
        og_desc_match = re.search(r'<meta[^>]*property="og:description"[^>]*content="([^"]*)"', html)
        if og_desc_match:
            og_desc = og_desc_match.group(1).strip()
        if not og_desc:
            desc_match = re.search(r'<meta[^>]*name="description"[^>]*content="([^"]*)"', html)
            if desc_match:
                og_desc = desc_match.group(1).strip()
        
        # 提取 cmt_id（评论组ID，用于获取热评）
        cmt_id = ""
        cmt_match = re.search(r'cmt_id\s*=\s*"([^"]+)"', html)
        if cmt_match:
            cmt_id = cmt_match.group(1)
        if not cmt_id:
            cmt_match2 = re.search(r'"comment_id"\s*:\s*"([^"]+)"', html)
            if cmt_match2:
                cmt_id = cmt_match2.group(1)
        
        # 解析 window.initData
        init_data = _extract_json_from_html(html, "window.initData")
        if init_data:
            content_data = init_data.get("content", {})
            if isinstance(content_data, dict):
                # 获取标题
                if not result["title"] and content_data.get("title"):
                    result["title"] = content_data["title"]
                
                # 从content_data中获取cmt_id（备选）
                if not cmt_id:
                    cmt_id = str(content_data.get("comment_id", "") or content_data.get("cmt_id", "") or "")
                
                atype = str(content_data.get("atype", ""))
                
                # 图文文章：尝试从各种字段获取正文
                article_body = ""
                
                # 从 content_data 直接获取正文字段
                for key in ["articleBody", "body", "text", "newsContent"]:
                    val = content_data.get(key)
                    if val and isinstance(val, str) and len(val) > 50:
                        article_body = _clean_html(val)
                        break
                
                # 从 content_list 获取
                if not article_body:
                    content_list = content_data.get("content_list", [])
                    if content_list and isinstance(content_list, list):
                        parts = []
                        for item in content_list:
                            if isinstance(item, dict):
                                t = item.get("content", "") or item.get("text", "") or item.get("value", "")
                                if t and isinstance(t, str) and len(t.strip()) > 5:
                                    cleaned = _clean_html(t)
                                    if cleaned:
                                        parts.append(cleaned)
                        if parts:
                            article_body = "\n".join(parts)
                
                # 摘要作为备选
                abstract = content_data.get("abstract", "")
                
                if article_body and len(article_body) > 30:
                    result["content"] = article_body
                    return result
                
                # 视频文章特殊处理（atype=4）
                if atype == "4":
                    # 构造视频文章的描述文本 —— 尽量丰富
                    video_parts = []
                    video_parts.append("[视频文章]")
                    
                    if result["title"]:
                        video_parts.append(f"标题：{result['title']}")
                    
                    if abstract:
                        video_parts.append(f"摘要：{abstract}")
                    
                    # og:description 通常比abstract更详细
                    if og_desc and og_desc != abstract and len(og_desc) > 10:
                        video_parts.append(f"内容描述：{og_desc}")
                    
                    # 发布时间
                    pub_time = content_data.get("time", "")
                    if pub_time:
                        video_parts.append(f"发布时间：{pub_time}")
                    
                    # 获取频道/作者信息
                    card = content_data.get("card", {})
                    if card:
                        chlname = card.get("chlname", "")
                        desc = card.get("desc", "")
                        vip_desc = card.get("vip_desc", "")
                        if chlname:
                            video_parts.append(f"频道：{chlname}")
                        if desc:
                            video_parts.append(f"频道简介：{desc}")
                        if vip_desc and vip_desc != desc:
                            video_parts.append(f"作者认证：{vip_desc}")
                    
                    # 来源
                    source_name = content_data.get("source", "")
                    if source_name and source_name != card.get("chlname", ""):
                        video_parts.append(f"来源：{source_name}")
                    
                    # 作者信息（另一种结构）
                    media_info = content_data.get("media", {})
                    if isinstance(media_info, dict):
                        media_name = media_info.get("name", "") or media_info.get("nick", "")
                        media_desc = media_info.get("desc", "") or media_info.get("introduction", "")
                        if media_name and media_name != card.get("chlname", ""):
                            video_parts.append(f"作者：{media_name}")
                        if media_desc and media_desc != card.get("desc", ""):
                            video_parts.append(f"作者简介：{media_desc}")
                    
                    # 视频信息 - 从 video_channel 获取
                    video_channel = content_data.get("video_channel", {})
                    if isinstance(video_channel, dict):
                        video_obj = video_channel.get("video", {})
                        if isinstance(video_obj, dict):
                            v_desc = video_obj.get("desc", "") or video_obj.get("description", "")
                            v_duration = video_obj.get("duration", "")
                            if v_desc:
                                video_parts.append(f"视频描述：{v_desc}")
                            if v_duration:
                                video_parts.append(f"视频时长：{v_duration}")
                    
                    # 备选视频信息字段
                    if not video_channel:
                        video_info = content_data.get("videoInfo") or content_data.get("videoNewsInfo") or {}
                        if isinstance(video_info, dict):
                            v_desc = video_info.get("desc", "") or video_info.get("description", "")
                            v_duration = video_info.get("duration", "") or video_info.get("time", "")
                            if v_desc:
                                video_parts.append(f"视频描述：{v_desc}")
                            if v_duration:
                                video_parts.append(f"视频时长：{v_duration}")
                    
                    # 获取评论数、点赞等互动信息
                    comments_count = content_data.get("comments", "") or content_data.get("comment_num", "")
                    if comments_count:
                        video_parts.append(f"评论数：{comments_count}")
                    
                    like_count = content_data.get("like_count", "") or content_data.get("praiseTimes", "") or content_data.get("likeInfo", "")
                    if like_count and like_count != 1:  # likeInfo=1 means enabled, not count
                        video_parts.append(f"点赞数：{like_count}")
                    
                    share_count = content_data.get("share_count", "")
                    if share_count:
                        video_parts.append(f"分享数：{share_count}")
                    
                    collect_count = content_data.get("collect_count", "")
                    if collect_count:
                        video_parts.append(f"收藏数：{collect_count}")
                    
                    # === 核心：AI内容标签（tag_news_rec）—— 最有价值的语义信息 ===
                    tag_news_rec = content_data.get("tag_news_rec", {})
                    if isinstance(tag_news_rec, dict):
                        rec_tags = tag_news_rec.get("tags", [])
                        if rec_tags and isinstance(rec_tags, list):
                            tag_names = [t.get("name", "") for t in rec_tags if isinstance(t, dict) and t.get("name")]
                            if tag_names:
                                video_parts.append(f"内容标签：{'、'.join(tag_names)}")
                    
                    # 相关标签/关键词（tag_info_item）
                    tag_info = content_data.get("tag_info_item", {})
                    if isinstance(tag_info, list):
                        tags = [t.get("name", "") for t in tag_info if isinstance(t, dict) and t.get("name")]
                        if tags:
                            video_parts.append(f"话题标签：{'、'.join(tags)}")
                    elif isinstance(tag_info, dict) and tag_info.get("name"):
                        video_parts.append(f"话题标签：{tag_info['name']}")
                    
                    # keywords字段
                    keywords = content_data.get("keywords", "") or content_data.get("keyword", "")
                    if keywords and isinstance(keywords, str) and keywords.strip():
                        video_parts.append(f"关键词：{keywords}")
                    elif keywords and isinstance(keywords, list):
                        video_parts.append(f"关键词：{'、'.join(keywords)}")
                    
                    # 专题信息（match_info）
                    match_info = content_data.get("match_info", {})
                    if isinstance(match_info, dict):
                        match_content = match_info.get("content", "")
                        if match_content and len(match_content.strip()) > 2:
                            video_parts.append(f"所属专题：{match_content}")
                    
                    # 相关事件线（relate_eventinfos）— 可能包含关键词和背景
                    relate_events = content_data.get("relate_eventinfos", [])
                    if isinstance(relate_events, list):
                        for event in relate_events[:2]:
                            if isinstance(event, dict):
                                basic = event.get("basic", {})
                                if isinstance(basic, dict):
                                    event_name = basic.get("name", "") or basic.get("event_name", "")
                                    if event_name:
                                        video_parts.append(f"相关事件：{event_name}")
                    
                    # 分享信息中的副标题（有时包含额外描述）
                    share_info = content_data.get("shareInfo", {})
                    if isinstance(share_info, dict):
                        share_subtitle = share_info.get("shareSubTitle", "")
                        if share_subtitle and "【视频】" in share_subtitle:
                            # 去掉"【视频】作者：xxx"这种无用信息
                            pass
                        elif share_subtitle and len(share_subtitle) > 10:
                            video_parts.append(f"分享描述：{share_subtitle}")
                    
                    # IP所在地
                    user_address = content_data.get("userAddress", "")
                    if user_address:
                        video_parts.append(f"发布地：{user_address}")
                    
                    # === 核心增强：抓取热门评论 ===
                    # 优先使用 commentid 字段
                    effective_cmt_id = cmt_id or str(content_data.get("commentid", ""))
                    try:
                        hot_comments = _fetch_qq_comments(article_id, effective_cmt_id, timeout=8)
                        if hot_comments:
                            video_parts.append(f"\n--- 热门评论（共{len(hot_comments)}条）---")
                            for i, comment in enumerate(hot_comments[:10], 1):
                                video_parts.append(f"  {i}. {comment}")
                    except Exception as e:
                        logger.info(f"Fetch hot comments failed: {e}")
                    
                    if len(video_parts) > 2:  # 至少有类型标记+标题+其他信息
                        result["content"] = "\n".join(video_parts)
                        result["is_video"] = True
                        return result
                
                # 非视频但没找到正文，用abstract
                if abstract and len(abstract) > 20:
                    result["content"] = abstract
                    return result
        
        # === initData解析失败的备选：直接从HTML提取 ===
        # 尝试各种正文容器
        selectors = [
            r'class="[^"]*article[-_]?content[^"]*"[^>]*>([\s\S]*?)</div>',
            r'class="[^"]*rich[-_]?text[^"]*"[^>]*>([\s\S]*?)</div>',
            r'class="[^"]*content[-_]?text[^"]*"[^>]*>([\s\S]*?)</div>',
            r'<article[^>]*>([\s\S]*?)</article>',
        ]
        for pattern in selectors:
            m = re.search(pattern, html, re.DOTALL)
            if m:
                text = _clean_html(m.group(1))
                if len(text) > 80:
                    result["content"] = text
                    return result
        
        # 从HTML中搜索 "content":"..." 字段（可能是内嵌的JSON数据）
        content_match = re.search(r'"(?:content|articleBody|newsContent)"\s*:\s*"((?:[^"\\]|\\.){80,})"', html)
        if content_match:
            raw_content = content_match.group(1)
            try:
                decoded = raw_content.encode('utf-8').decode('unicode_escape')
            except:
                decoded = raw_content
            cleaned = _clean_html(decoded)
            if len(cleaned) > 50:
                result["content"] = cleaned
                return result
                
    except Exception as e:
        logger.info(f"QQ News HTML parse failed: {e}")

    # === 备选方案：尝试旧API（可能仍对部分文章有效） ===
    if article_id:
        api_urls = [
            f"https://r.inews.qq.com/getSimpleNews?id={article_id}",
            f"https://i.news.qq.com/trpc.qqnews_web.kv_cache.KvCache/GetNewsContent?msg_id={article_id}",
        ]
        for api_url in api_urls:
            try:
                raw = _make_request(api_url, headers=QQ_API_HEADERS, timeout=timeout)
                text = raw.decode("utf-8")
                data = json.loads(text)
                
                title = data.get("title") or data.get("data", {}).get("title", "")
                if title and not result["title"]:
                    result["title"] = title
                
                content_text = ""
                content_obj = data.get("content", {})
                if isinstance(content_obj, dict) and content_obj.get("text"):
                    content_text = content_obj["text"]
                if not content_text:
                    content_text = data.get("data", {}).get("content_html", "")
                if not content_text:
                    dc = data.get("data", {}).get("content", "")
                    if isinstance(dc, str) and len(dc) > 30:
                        content_text = dc
                
                if content_text:
                    cleaned = re.sub(r'<!--(VIDEO|IMG|AD|AIPOS)_\d+-->', '', content_text).strip()
                    if len(cleaned) > 20:
                        result["content"] = _clean_html(content_text)
                        return result
                
                if not result["content"] and data.get("abstract"):
                    result["content"] = data["abstract"]
                    return result
            except Exception:
                continue

    return result


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
    content_preview = content[:1200] if content else "（无正文）"
    
    # 检测是否为视频文章（包含热评参考）
    is_video_with_comments = "[视频文章]" in content and "热门评论" in content
    is_video_without_comments = "[视频文章]" in content and "热门评论" not in content

    system_prompt = """你是一个互联网评论模拟器。你的唯一任务是输出真实用户风格的评论。
规则：
- 每条评论像真人随手打的，有口语感、有情绪、有瑕疵
- 禁止出现：值得关注、让我们拭目以待、不禁令人深思、综上所述、作为一个XX
- 可以有：语气词(哈哈/啊/吧/嘛/草)、省略号、错别字、网络用语、表情
- 风格差异要大，像完全不同的人写的
- 严格按JSON格式输出，不要有任何多余解释"""

    # 视频文章增强prompt
    if is_video_with_comments:
        extra_instruction = """
注意：这是一个视频文章，我提供了该视频下的真实热门评论作为参考。
请你：
1. 通过标题+描述+热门评论来推断视频的核心内容和讨论点
2. 生成的评论要紧扣视频实际讨论的话题，不能泛泛而谈
3. 可以借鉴热评的讨论方向，但不要复制热评内容
4. 评论要像看完这个视频后的真实反应"""
    elif is_video_without_comments:
        extra_instruction = """
注意：这是一个视频文章，无法获取视频语音转文字内容，但我提供了视频的元数据信息（标题、内容标签、频道、作者等）。
请你：
1. 根据标题和内容标签推断视频可能讨论的主题和情感基调
2. 想象自己看完了这个视频后会产生什么真实反应
3. 评论要具体、有针对性，围绕标题暗示的核心故事展开
4. 避免太泛的"加油""好棒"类无信息量评论，要结合具体场景
5. 可以从以下角度切入：共情故事主人公的经历、质疑转行的勇气、分享自己的类似经历、调侃或幽默回应"""
    else:
        extra_instruction = ""

    user_prompt = f"""为这篇文章生成{count}条拟人评论：

标题：{title}
正文：{content_preview}
垂类：{category}
{f'基调偏好：{tone}' if tone else ''}
{f'特殊要求：{requirements}' if requirements else ''}
{extra_instruction}

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

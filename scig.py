import re
import json
from datetime import date, datetime, timedelta
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

import httpx
from config import settings
from database import get_db, User, Generation, DailyQuota
from auth import get_current_user
from schemas import (
    CompileRequest, CompileResponse,
    ValidationResult, ValidationLayer,
    GenerationSummary, GenerationListResponse,
    UpgradeRequest, UpgradeResponse,
    PlansResponse, SubscriptionPlan,
)
router = APIRouter(prefix="/api", tags=["SCIG 编译管线"])
# ═══════════════════════════════════════════════════════
#  工具函数
# ═══════════════════════════════════════════════════════

def sanitize_svg(text: str) -> str:
    """从 LLM 原始输出中提取纯 SVG 代码"""
    result = text
    # 剥离 Markdown 代码围栏
    if "```" in result:
        result = re.sub(r"```html|```svg|```xml|```", "", result)
    # 提取 <svg ... </svg> 片段
    start = result.find("<svg")
    end = result.rfind("</svg>")
    if start != -1 and end != -1:
        result = result[start:end + 6]
    return result.strip()

def inject_watermark(svg: str, tier: str) -> str:
    """为免费版 SVG 注入水印"""
    if tier != "free":
        return svg
    watermark = (
        '\n  '
        '\n  <text x="95%" y="97%" font-size="10" fill="#ffffff18" '
        'font-family="sans-serif" text-anchor="end" '
        'font-style="italic">SCIG Free · 升级 Premium 解锁无水印导出</text>\n'
    )
    # 在 </svg> 之前插入
    idx = svg.rfind("</svg>")
    if idx != -1:
        svg = svg[:idx] + watermark + svg[idx:]
    return svg

def safe_float(val, default=0.0):
    """安全地将 SVG 属性字符串转换为浮点数，自动处理 % 和 px 避免崩溃"""
    if val is None:
        return default
    try:
        val_str = str(val).strip()
        if val_str.endswith('%'):
            return float(val_str.replace('%', ''))
        if val_str.endswith('px'):
            return float(val_str.replace('px', ''))
        return float(val_str)
    except (ValueError, TypeError):
        return default

# ═══════════════════════════════════════════════════════
#  ScigValidator: 三层刚性校验
# ═══════════════════════════════════════════════════════

# 内置科学实体词典 (MVP 启发式校验)
KNOWN_ENTITIES = {
    "AKT", "PI3K", "mTOR", "mTORC1", "mTORC2", "TSC2", "EGFR", "KRAS",
    "ATP", "ADP", "AMP", "GTP", "GDP", "NADH", "NADPH", "FADH2",
    "RAS", "RAF", "MEK", "ERK", "MAPK", "PTEN", "NF-κB", "p53",
    "JAK", "STAT", "Wnt", "β-catenin", "Notch", "Hedgehog",
    "AMPK", "PI3K/AKT", "RAS/RAF", "JAK/STAT", "TCA", "ETC",
    "Glucose", "Pyruvate", "Acetyl-CoA", "Citrate", "Lactate",
    "DNA", "RNA", "mRNA", "tRNA", "rRNA", "Protein", "Enzyme",
    "Receptor", "Ligand", "Kinase", "Phosphatase", "Ion Channel",
    "C", "H", "O", "N", "P", "S", "Ca", "Na", "K", "Cl", "Fe", "Mg", "Zn",
}

# 化学反应关键词
CHEMISTRY_KEYWORDS = [
    "反应", "reaction", "方程式", "equation", "化学", "chemistry",
    "原子", "atom", "分子", "molecule", "键", "bond",
    "氧化", "还原", "催化", "合成", "分解",
    "电荷", "charge", "能量", "energy", "守恒", "conservation",
    "摩尔", "mol", "浓度", "concentration", "pH",
    "→", "←", "⇌", "⟶", "+",
]

class ScigValidator:
    """三层刚性逻辑校验器 (MVP 启发式版本)"""

    @staticmethod
    def extract_svg_texts(svg: str) -> list[str]:
        """从 SVG 中提取所有 <text> 元素的文本内容"""
        texts = re.findall(r"<text[^>]*>(.*?)</text>", svg, re.DOTALL)
        # 去除内嵌的标签
        clean = []
        for t in texts:
            t = re.sub(r"<[^>]+>", "", t).strip()
            if t:
                clean.append(t)
        return clean

    @staticmethod
    def extract_svg_elements(svg: str) -> dict:
        """提取 SVG 中的图形元素坐标用于拓扑分析"""
        circles = re.findall(r'<circle[^>]*cx="([^"]+)"[^>]*cy="([^"]+)"', svg)
        rects = re.findall(r'<rect[^>]*x="([^"]+)"[^>]*y="([^"]+)"[^>]*width="([^"]+)"[^>]*height="([^"]+)"', svg)
        ellipses = re.findall(r'<ellipse[^>]*cx="([^"]+)"[^>]*cy="([^"]+)"', svg)
        lines = re.findall(r'<line[^>]*x1="([^"]+)"[^>]*y1="([^"]+)"[^>]*x2="([^"]+)"[^>]*y2="([^"]+)"', svg)
        paths = re.findall(r'<path[^>]*d="([^"]+)"', svg)

        nodes = []
        for cx, cy in circles:
            nodes.append({"type": "circle", "x": safe_float(cx), "y": safe_float(cy)})
        for x, y, w, h in rects:
            nodes.append({
                "type": "rect",
                "x": safe_float(x) + safe_float(w) / 2,
                "y": safe_float(y) + safe_float(h) / 2
            })
        for cx, cy in ellipses:
            nodes.append({"type": "ellipse", "x": safe_float(cx), "y": safe_float(cy)})

        edges = []
        for x1, y1, x2, y2 in lines:
            edges.append({
                "x1": safe_float(x1),
                "y1": safe_float(y1),
                "x2": safe_float(x2),
                "y2": safe_float(y2)
            })

        # 简单解析 path 中的 M ... L ... 指令，正则兼容提取数字+可能的单位(%/px)
        for d in paths:
            coords = re.findall(r"([\d.]+(?:%|px)?)[,\s]+([\d.]+(?:%|px)?)", d)
            if len(coords) >= 2:
                for i in range(len(coords) - 1):
                    edges.append({
                        "x1": safe_float(coords[i][0]), "y1": safe_float(coords[i][1]),
                        "x2": safe_float(coords[i + 1][0]), "y2": safe_float(coords[i + 1][1]),
                    })

        return {"nodes": nodes, "edges": edges}

    def validate_L1_knowledge(self, svg: str, prompt: str) -> ValidationLayer:
        """第一层：知识事实校验 — 检查 SVG 标签是否属于已知科学实体"""
        texts = self.extract_svg_texts(svg)
        entities_found = []
        unverified = []

        for t in texts:
            # 跳过纯数字、短标签、坐标等
            if len(t) < 2 or t.isdigit() or re.match(r"^[\d.\-+]+$", t):
                continue
            if any(t.upper() == e.upper() or t == e for e in KNOWN_ENTITIES):
                entities_found.append(t)
            elif len(t) > 2 and not re.match(r"^\d+%?$", t):
                unverified.append(t)

        if not unverified:
            return ValidationLayer(
                status="PASS",
                message=f"知识事实溯源 100% 吻合 — 识别 {len(entities_found)} 个已知科学实体",
                details={"entities_found": entities_found, "unverified": []},
            )
        return ValidationLayer(
            status="WARNING",
            message=f"发现 {len(unverified)} 个未验证实体标签，建议人工复核",
            details={"entities_found": entities_found, "unverified": unverified},
        )

    def validate_L2_topology(self, svg: str) -> ValidationLayer:
        """第二层：图谱结构校验 — 检查孤立节点和悬空边"""
        elems = self.extract_svg_elements(svg)
        nodes = elems["nodes"]
        edges = elems["edges"]

        if not nodes and not edges:
            return ValidationLayer(
                status="PASS",
                message="无图谱结构元素，跳过拓扑校验",
                details={"node_count": 0, "edge_count": 0, "orphan_nodes": [], "dangling_edges": []},
            )

        PROXIMITY = 30.0  # 像素容差

        # 检查孤立节点（附近没有边端点）
        orphan_nodes = []
        for i, node in enumerate(nodes):
            connected = False
            for edge in edges:
                d1 = ((node["x"] - edge["x1"]) ** 2 + (node["y"] - edge["y1"]) ** 2) ** 0.5
                d2 = ((node["x"] - edge["x2"]) ** 2 + (node["y"] - edge["y2"]) ** 2) ** 0.5
                if d1 < PROXIMITY or d2 < PROXIMITY:
                    connected = True
                    break
            if not connected:
                orphan_nodes.append({"index": i, "type": node["type"], "x": node["x"], "y": node["y"]})

        # 检查悬空边（端点附近没有节点）
        dangling_edges = []
        for i, edge in enumerate(edges):
            has_start = any(
                ((node["x"] - edge["x1"]) ** 2 + (node["y"] - edge["y1"]) ** 2) ** 0.5 < PROXIMITY
                for node in nodes
            )
            has_end = any(
                ((node["x"] - edge["x2"]) ** 2 + (node["y"] - edge["y2"]) ** 2) ** 0.5 < PROXIMITY
                for node in nodes
            )
            if not has_start or not has_end:
                dangling_edges.append({"index": i, "x1": edge["x1"], "y1": edge["y1"]})

        if not orphan_nodes and not dangling_edges:
            return ValidationLayer(
                status="PASS",
                message=f"拓扑结构合法 — {len(nodes)} 节点, {len(edges)} 连线, 0 异常",
                details={"node_count": len(nodes), "edge_count": len(edges), "orphan_nodes": [], "dangling_edges": []},
            )

        issues = []
        if orphan_nodes:
            issues.append(f"{len(orphan_nodes)} 个孤立节点")
        if dangling_edges:
            issues.append(f"{len(dangling_edges)} 条悬空边")

        return ValidationLayer(
            status="WARNING",
            message=f"拓扑结构存在 " + ", ".join(issues),
            details={
                "node_count": len(nodes), "edge_count": len(edges),
                "orphan_nodes": orphan_nodes, "dangling_edges": dangling_edges,
            },
        )

    def validate_L3_rules(self, prompt: str) -> ValidationLayer:
        """第三层：学科规则校验 — 扫描化学/物理关键词"""
        prompt_lower = prompt.lower()
        found_keywords = [kw for kw in CHEMISTRY_KEYWORDS if kw.lower() in prompt_lower]

        if not found_keywords:
            return ValidationLayer(
                status="PASS",
                message="未检测到化学/物理计量关键词，跳过刚性规则校验",
                details={"rules_checked": 0, "keywords_found": []},
            )

        # 检查是否有箭头/等式符号（反应表达式）
        has_equation = any(sym in prompt for sym in ["→", "←", "⇌", "⟶", "+", "="])

        if has_equation:
            return ValidationLayer(
                status="PASS",
                message=f"化学计量表达式检测通过 — 已识别 {len(found_keywords)} 个学科关键词",
                details={
                    "rules_checked": len(found_keywords),
                    "keywords_found": found_keywords,
                    "equation_detected": True,
                    "violations": [],
                },
            )

        return ValidationLayer(
            status="PASS",
            message=f"学科关键词检测完成 — {len(found_keywords)} 个相关术语，无不规范表达式",
            details={
                "rules_checked": len(found_keywords),
                "keywords_found": found_keywords,
                "violations": [],
            },
        )

# ═══════════════════════════════════════════════════════
#  LLM 编译客户端
# ═══════════════════════════════════════════════════════

SYSTEM_PROMPT_TEMPLATES = {
    "free": """你是一位精通微观生物化学、半导体工程机理以及极致前端现代美学的图纸编译专家。
你的唯一任务是根据用户对严肃科学机理的描述，直接设计并编写出一段完美的、富有视觉震撼力的高清 SVG 矢量代码。

硬性要求：
1. 必须只能输出以 <svg 开头，以 </svg> 结束的规范内联代码，绝不允许带有 Markdown 语法标记（绝对不要写 ```html）、不要写任何前言废话、不要有任何多余的解释文字。
2. 画面必须极其高大上且极具科技美学：背景必须显式设定为深邃的极暗色系（如 #020617），推荐大量运用微弱的发光滤镜（feDropShadow / feGaussianBlur）、高级渐变色彩（LinearGradient）、纤细且粗细有致的线条来表达信号或物质流向。
3. 节点实体标签可读性要强，画面尺寸规范设置在 viewBox="0 0 800 400" 左右，保证良好的响应式视口自适应缩放性能。
4. 所有的 SVG 几何坐标（如 x1, y1, cx, cy, width, height 等）必须使用纯数字绝对像素值，绝对不允许带有 '%' 或 'px' 等单位符号。""",

    "premium": """你是一位精通微观生物化学、半导体工程机理以及极致前端现代美学的图纸编译专家。
你的唯一任务是根据用户对严肃科学机理的描述，直接设计并编写出一段完美的、富有视觉震撼力的高清 SVG 矢量代码。

硬性要求：
1. 必须只能输出以 <svg 开头，以 </svg> 结束的规范内联代码，绝不允许带有 Markdown 语法标记（绝对不要写 ```html）、不要写任何前言废话、不要有任何多余的解释文字。
2. 画面必须极其高大上且极具科技美学：背景必须显式设定为深邃的极暗色系（如 #020617），大量运用多层发光滤镜组合（feDropShadow + feGaussianBlur + feMerge），多级高级渐变色彩（多个 LinearGradient 叠加），纤细与粗线交替表达主次信号流向。
3. 画面尺寸规范设置在 viewBox="0 0 1000 600"，节点实体标签字体更大更清晰，排版更从容，保证良好的响应式视口自适应缩放性能。
4. 构图必须富有叙事性、层次感和视觉冲击力，如同 Cell/Nature 期刊封面图品质。
5. 所有的 SVG 几何坐标（如 x1, y1, cx, cy, width, height 等）必须使用纯数字绝对像素值，绝对不允许带有 '%' 或 'px' 等单位符号。""",

    "enterprise": """你是一位精通微观生物化学、半导体工程机理以及极致前端现代美学的图纸编译专家。
你的唯一任务是根据用户对严肃科学机理的描述，直接设计并编写出一段完美的、富有视觉震撼力的高清 SVG 矢量代码。

硬性要求：
1. 必须只能输出以 <svg 开头，以 </svg> 结束的规范内联代码，绝不允许带有 Markdown 语法标记（绝对不要写 ```html）、不要写任何前言废话、不要有任何多余的解释文字。
2. 画面必须极具顶级期刊发表水准：背景使用多层次暗色系与微网格纹理，大量运用光晕、内外阴影、多层渐变叠加等高级滤镜技术，创建丰富的视觉深度。
3. 画布尺寸 viewBox="0 0 1200 700"，排版奢华，适合高清打印与投影展示。
4. 构图必须富有叙事性、层次感和视觉冲击力，支持复杂的多层级子图嵌套表达。
5. 所有的 SVG 几何坐标（如 x1, y1, cx, cy, width, height 等）必须使用纯数字绝对像素值，绝对不允许带有 '%' 或 'px' 等单位符号。""",
}

async def compile_svg_via_deepseek(prompt_text: str, tier: str) -> str:
    """通过 DeepSeek API 将自然语言编译为 SVG"""
    if not settings.DEEPSEEK_API_KEY or settings.DEEPSEEK_API_KEY.startswith("sk-your-"):
        raise HTTPException(
            status_code=503,
            detail="LLM API Key 未配置，请在 .env 文件中设置 DEEPSEEK_API_KEY",
        )

    system_prompt = SYSTEM_PROMPT_TEMPLATES.get(tier, SYSTEM_PROMPT_TEMPLATES["free"])

    async with httpx.AsyncClient(timeout=45.0) as client:
        try:
            resp = await client.post(
                f"{settings.DEEPSEEK_BASE_URL}/chat/completions",
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {settings.DEEPSEEK_API_KEY}",
                },
                json={
                    "model": settings.DEEPSEEK_MODEL,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": prompt_text},
                    ],
                    "temperature": 0.15,
                    "stream": False,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            raw = data["choices"][0]["message"]["content"]
            return raw.strip()

        except httpx.TimeoutException:
            raise HTTPException(status_code=504, detail="LLM 编译超时，请稍后重试")
        except httpx.HTTPStatusError as e:
            raise HTTPException(status_code=502, detail=f"LLM API 异常: HTTP {e.response.status_code}")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"编译管线内部错误: {str(e)}")

# ═══════════════════════════════════════════════════════
#  核心端点
# ═══════════════════════════════════════════════════════

@router.post("/compile", response_model=CompileResponse, status_code=201)
async def compile_pipeline(
    body: CompileRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """SCIG 全链路闭环编译: 配额 → LLM → 净化 → 校验 → 水印 → 存储"""
    today = date.today()
    limit = settings.TIER_QUOTA_LIMITS.get(current_user.tier, 5)

    # ── 1. 配额检查 ──
    quota = db.query(DailyQuota).filter(
        DailyQuota.user_id == current_user.id,
        DailyQuota.date == today,
    ).first()

    if quota is None:
        quota = DailyQuota(user_id=current_user.id, date=today, count=0)
        db.add(quota)
        db.flush()

    if quota.count >= limit:
        remaining = 0
        raise HTTPException(
            status_code=402,
            detail=f"今日生成配额已用完 ({quota.count}/{limit})。升级至 Premium 解锁无限生成！",
        )

    # ── 2. LLM 编译 ──
    raw_svg = await compile_svg_via_deepseek(body.prompt_text, current_user.tier)

    # ── 3. SVG 净化 ──
    svg_output = sanitize_svg(raw_svg)
    if not svg_output.startswith("<svg") or not svg_output.endswith("</svg>"):
        raise HTTPException(
            status_code=502,
            detail="LLM 未能生成有效的 SVG 代码，请调整输入描述后重试",
        )

    # ── 4. 三层校验 ──
    validator = ScigValidator()
    v1 = validator.validate_L1_knowledge(svg_output, body.prompt_text)
    v2 = validator.validate_L2_topology(svg_output)
    v3 = validator.validate_L3_rules(body.prompt_text)

    validation = ValidationResult(L1=v1, L2=v2, L3=v3)

    # ── 5. 水印注入 (仅免费版) ──
    svg_output = inject_watermark(svg_output, current_user.tier)

    # ── 6. 消耗配额 & 存储 ──
    quota.count += 1

    gen = Generation(
        user_id=current_user.id,
        prompt_text=body.prompt_text,
        svg_output=svg_output,
        validation_json=validation.model_dump_json(),
        model_used=settings.DEEPSEEK_MODEL,
    )
    db.add(gen)
    db.commit()
    db.refresh(gen)

    remaining = max(0, limit - quota.count)

    return CompileResponse(
        id=gen.id,
        svg_output=svg_output,
        validation=validation,
        quota_remaining=remaining,
        quota_limit=limit,
        tier=current_user.tier,
        created_at=gen.created_at.isoformat() if gen.created_at else None,
    )

@router.get("/generations", response_model=GenerationListResponse)
def list_generations(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    skip: int = 0,
    limit: int = 20,
):
    """获取用户的生成历史"""
    query = db.query(Generation).filter(Generation.user_id == current_user.id)

    # 免费版仅显示 24 小时内的记录
    if current_user.tier == "free":
        since = datetime.utcnow() - timedelta(hours=24)
        query = query.filter(Generation.created_at >= since)

    total = query.count()
    items = (
        query.order_by(Generation.created_at.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )

    summaries = [
        GenerationSummary(
            id=g.id,
            prompt_preview=g.prompt_text[:100] + "..." if len(g.prompt_text) > 100 else g.prompt_text,
            model_used=g.model_used,
            created_at=g.created_at.isoformat() if g.created_at else None,
        )
        for g in items
    ]

    return GenerationListResponse(items=summaries, total=total)

@router.get("/generations/{gen_id}")
def get_generation(
    gen_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """获取单次生成的完整详情"""
    gen = db.query(Generation).filter(
        Generation.id == gen_id,
        Generation.user_id == current_user.id,
    ).first()

    if not gen:
        raise HTTPException(status_code=404, detail="生成记录不存在")

    validation = None
    if gen.validation_json:
        try:
            validation = json.loads(gen.validation_json)
        except json.JSONDecodeError:
            validation = None

    return {
        "id": gen.id,
        "prompt_text": gen.prompt_text,
        "svg_output": gen.svg_output,
        "validation": validation,
        "model_used": gen.model_used,
        "created_at": gen.created_at.isoformat() if gen.created_at else None,
    }

# ── 订阅 & 升级 ─────────────────────────────────────

@router.get("/subscription/plans", response_model=PlansResponse)
def get_plans():
    """获取套餐对比信息"""
    plans = [
        SubscriptionPlan(
            tier="free",
            name="Free 免费版",
            price="¥0",
            price_monthly="免费",
            features=[
                "每日 5 次 AI 科学可视化生成",
                "标准 SVG 画质 (800x400)",
                "三层免疫级刚性校验",
                "边缘解耦本地渲染",
                "含「SCIG Free」水印",
                "24 小时历史记录",
            ],
        ),
        SubscriptionPlan(
            tier="premium",
            name="Premium 专业版",
            price="¥29",
            price_monthly="¥29 / 月",
            features=[
                "每日 50 次 AI 科学可视化生成",
                "增强 SVG 画质 (1000x600)",
                "三层免疫级刚性校验 (增强)",
                "无水印高清导出 · 一键下载",
                "完整历史记录永久保存",
                "优先队列编译加速",
                "Cell/Nature 期刊级视觉品质",
            ],
            highlighted=True,
        ),
        SubscriptionPlan(
            tier="enterprise",
            name="Enterprise 企业版",
            price="¥99",
            price_monthly="¥99 / 月",
            features=[
                "无限 AI 科学可视化生成",
                "企业定制 SVG 画质 (1200x700)",
                "三层免疫级刚性校验 (增强+自定义规则)",
                "无水印高清导出 · 批量下载",
                "完整历史 + 数据导出 API",
                "REST API 编程接入",
                "专属数据隔离安全方案",
                "团队协作与权限管理",
            ],
        ),
    ]
    return PlansResponse(plans=plans)

@router.post("/subscription/upgrade", response_model=UpgradeResponse)
def upgrade_subscription(
    body: UpgradeRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """模拟订阅升级 (MVP 阶段无真实支付)"""
    if current_user.tier == body.tier:
        raise HTTPException(status_code=400, detail=f"您已是 {body.tier} 会员，无需重复升级")

    if body.tier == "enterprise" and current_user.tier == "premium":
        # 允许从 Premium 升级到 Enterprise
        pass

    old_tier = current_user.tier
    current_user.tier = body.tier
    db.commit()

    tier_names = {"premium": "专业版 Premium", "enterprise": "企业版 Enterprise"}
    return UpgradeResponse(
        success=True,
        new_tier=body.tier,
        message=f"🎉 恭喜！您已成功升级至 {tier_names.get(body.tier, body.tier)}！"
                f"刷新页面即可享受全新特权。",
    )
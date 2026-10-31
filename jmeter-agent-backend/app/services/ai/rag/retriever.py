"""RAG retriever — pre-analysis keyword extraction and historical search.

Flow:
1. Extract module, version, defect keywords from new test report
2. Search RAG store for matching historical documents
3. Return formatted context for augmented LLM analysis
"""

import re
import json
from typing import Optional

from app.services.ai.rag.store import LanceRAGStore, RAGDocument, rag_store


# Patterns for extracting structured info from report summaries
MODULE_PATTERNS = [
    r"模块[:：]\s*([^\s,，]+)",
    r"服务[:：]\s*([^\s,，]+)",
    r"接口[:：]\s*([^\s,，]+)",
    r"API[:：]\s*([^\s,，]+)",
    r"系统[:：]\s*([^\s,，]+)",
    r"项目[:：]\s*([^\s,，]+)",
]

VERSION_PATTERNS = [
    r"版本[:：]\s*([^\s,，]+)",
    r"v(\d+\.\d+\.\d+)",
    r"version[:：]\s*([^\s,，]+)",
]

DEFECT_KEYWORDS = [
    "超时", "timeout", "错误", "error", "失败", "failed",
    "延迟", "latency", "瓶颈", "bottleneck", "内存泄漏", "memory leak",
    "CPU", "连接池", "connection pool", "死锁", "deadlock",
    "OOM", "GC", "503", "502", "500", "429", "雪崩", "avalanche",
    "限流", "rate limit", "熔断", "circuit breaker",
    "吞吐量下降", "throughput drop", "响应时间飙升", "response time spike",
]


class RAGRetriever:
    """Pre-analysis retrieval: extract context from new report, search history."""

    def __init__(self, store: Optional[LanceRAGStore] = None):
        self.store = store or rag_store

    def extract_metadata(self, report_data: dict) -> dict:
        """Extract module, version, and defect keywords from raw report data.

        Args:
            report_data: The test summary dict (from parse_jtl_report or execution summary).

        Returns:
            Dict with keys: module, version, defect_keywords, test_type, all_keywords.
        """
        # Flatten report to text for pattern matching
        text = json.dumps(report_data, ensure_ascii=False)

        module = self._extract_first(MODULE_PATTERNS, text, default="unknown")
        version = self._extract_first(VERSION_PATTERNS, text, default="unknown")
        test_type = report_data.get("test_type", report_data.get("test_config", {}).get("test_type", ""))

        # Extract defect keywords present in the report
        defect_kws = []
        text_lower = text.lower()
        for kw in DEFECT_KEYWORDS:
            if kw.lower() in text_lower:
                defect_kws.append(kw)

        # Build general keywords from report structure
        general_kws = []
        # Add error types if present
        for err_detail in report_data.get("error_details", []):
            err_type = err_detail.get("error_type", "")
            if err_type:
                general_kws.append(err_type)

        # Add percentile info as search terms
        percentiles = report_data.get("percentiles", {})
        if percentiles:
            p99 = percentiles.get("p99", 0)
            if p99 and p99 > 1000:
                general_kws.append("p99高延迟")

        all_keywords = list(set(defect_kws + general_kws))

        return {
            "module": module,
            "version": version,
            "test_type": test_type,
            "defect_keywords": defect_kws,
            "all_keywords": all_keywords,
        }

    def retrieve(
        self,
        report_data: dict,
        limit: int = 10,
        module: Optional[str] = None,
    ) -> dict:
        """Search RAG for relevant historical documents.

        Args:
            report_data: New test report data.
            limit: Max documents per category.
            module: The module when the caller knows it; otherwise it is guessed from the report text.

        Returns:
            Dict with: metadata, previous_analyses, similar_defects,
                       module_history, module_standards, formatted_context.
        """
        meta = self.extract_metadata(report_data)
        module = meta["module"] = module or meta["module"]
        keywords = meta["all_keywords"]

        # 1. Previous analysis conclusions for same module
        previous_analyses = self.store.hybrid_search(
            module=module,
            doc_type="analysis_conclusion",
            limit=limit,
        )

        # 2. Known defects matching the report's defect keywords
        similar_defects = self.store.hybrid_search(
            keywords=keywords,
            doc_type="defect_retrospective",
            limit=limit,
        )

        # 3. Known issues matching keywords
        known_issues = self.store.hybrid_search(
            keywords=keywords,
            doc_type="known_issue",
            limit=limit,
        )

        # 4. Module standards / baselines
        module_standards = self.store.hybrid_search(
            module=module,
            doc_type="module_standard",
            limit=limit,
        )

        # 5. Module version history (for trend comparison)
        module_versions = self.store.get_module_versions(module)

        # 6. Raw reports from same module for direct comparison
        raw_reports = self.store.hybrid_search(
            module=module,
            doc_type="raw_report",
            limit=5,
        )

        # Build formatted context for LLM injection
        formatted = self._format_context(
            meta=meta,
            previous_analyses=previous_analyses,
            similar_defects=similar_defects,
            known_issues=known_issues,
            module_standards=module_standards,
            module_versions=module_versions,
            raw_reports=raw_reports,
        )

        return {
            "metadata": meta,
            "previous_analyses": previous_analyses,
            "similar_defects": similar_defects,
            "known_issues": known_issues,
            "module_standards": module_standards,
            "module_versions": module_versions,
            "raw_reports": raw_reports,
            "formatted_context": formatted,
            "has_history": bool(previous_analyses or similar_defects or known_issues),
        }

    def _extract_first(self, patterns: list[str], text: str, default: str = "") -> str:
        """Extract first regex match from text."""
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return match.group(1).strip()
        return default

    def _format_context(
        self,
        meta: dict,
        previous_analyses: list[RAGDocument],
        similar_defects: list[RAGDocument],
        known_issues: list[RAGDocument],
        module_standards: list[RAGDocument],
        module_versions: list[str],
        raw_reports: list[RAGDocument],
    ) -> str:
        """Build a formatted context string for LLM prompt injection."""
        parts = []

        parts.append("## 历史知识库检索结果\n")

        # Module info
        parts.append(f"- **模块**: {meta['module']}")
        parts.append(f"- **版本**: {meta['version']}")
        parts.append(f"- **测试类型**: {meta.get('test_type', 'unknown')}")
        if meta.get("defect_keywords"):
            parts.append(f"- **检测到缺陷关键词**: {', '.join(meta['defect_keywords'])}")
        parts.append("")

        # Version history
        if module_versions:
            parts.append(f"### 该模块历史版本 ({len(module_versions)} 个)")
            parts.append(", ".join(module_versions[:10]))
            parts.append("")

        # Previous analyses
        if previous_analyses:
            parts.append(f"### 历史分析结论 ({len(previous_analyses)} 条)")
            for i, doc in enumerate(previous_analyses[:3], 1):
                parts.append(f"**{i}. [{doc.version}] {doc.title}** (创建于 {doc.created_at[:10]})")
                parts.append(doc.content[:800])
                parts.append("")
        else:
            parts.append("### 历史分析结论\n（无）\n")

        # Similar defects
        if similar_defects:
            parts.append(f"### 相关缺陷复盘 ({len(similar_defects)} 条)")
            for doc in similar_defects[:3]:
                parts.append(f"- [{doc.version}] {doc.title}: {doc.content[:300]}")
            parts.append("")

        # Known issues
        if known_issues:
            parts.append(f"### 高频已知问题 ({len(known_issues)} 条)")
            for doc in known_issues[:5]:
                parts.append(f"- {doc.title}: {doc.content[:200]}")
            parts.append("")

        # Module standards
        if module_standards:
            parts.append(f"### 模块性能标准 ({len(module_standards)} 条)")
            for doc in module_standards[:3]:
                parts.append(f"- [{doc.version}] {doc.title}")
                parts.append(doc.content[:400])
            parts.append("")

        # Previous raw reports for comparison
        if raw_reports:
            parts.append(f"### 历史原始报告 ({len(raw_reports)} 条，可用于对比)")
            for doc in raw_reports[:3]:
                parts.append(f"- [{doc.version}] {doc.title} ({doc.created_at[:10]})")
            parts.append("")

        return "\n".join(parts)

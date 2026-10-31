"""RAG archiver — post-analysis filtering and archival with anti-pollution rules.

Core principle: LLM output is NOT trusted as ground truth.
- Facts (numbers) are computed from JTL by code and stored in FactLedger.
- Interpretations (bottlenecks, root causes) are stored as hypotheses in HypothesisLedger,
  default status = untested.
- Free-text analysis_conclusion is archived ONLY if all numeric claims inside it pass
  FactValidator against the computed FactSheet.

Numeric validation covers recognized claims only, not every assertion in a report.
Interpretations remain untested hypotheses until supported by independent evidence.
"""

import re
import json
import logging
import hashlib
from datetime import datetime
from typing import Optional

from app.services.ai.rag.store import (
    LanceRAGStore, RAGDocument, ALLOWED_DOC_TYPES, rag_store,
)
from app.services.ai.memory import (
    fact_ledger, hypothesis_ledger, fact_validator, hypothesis_extractor,
)
from app.models.schemas import (
    FactSheet, StructuredAnalysis, Hypothesis, HypothesisStatus,
)

logger = logging.getLogger(__name__)


class RAGArchiver:
    """Controls what gets archived to the RAG knowledge base.

    Anti-pollution rules:
    1. Facts come from code, not LLM.
    2. Numeric claims in analysis text are validated against facts before archival.
    3. Interpretations are stored as structured hypotheses (status=untested).
    4. Free-text conclusions are stored only if validation passes.
    5. Raw reports are projections; original JTL is the immutable source of truth.
    """

    def __init__(self, store: Optional[LanceRAGStore] = None):
        self.store = store or rag_store

    # === New Anti-Pollution Archive Flow ===

    async def archive_analysis_result(
        self,
        structured_analysis: StructuredAnalysis,
        fact: FactSheet,
        extra_values: tuple = (),
    ) -> dict:
        """Archive a structured analysis result with full validation.

        Returns:
            {
                "validation": ValidationResult,
                "fact_archived": bool,
                "hypotheses_archived": list[str],
                "conclusion_archived": Optional[str],
                "raw_report_archived": Optional[str],
                "rejected_reason": Optional[str],
                "errors": list[str],
            }
        """
        result = {
            "validation": None,
            "fact_archived": False,
            "hypotheses_archived": [],
            "conclusion_archived": None,
            "raw_report_archived": None,
            "rejected_reason": None,
            "errors": [],
        }
        if not fact.context_scope or not fact.jtl_path or not fact.jtl_sha256:
            result['rejected_reason'] = 'Automatic archival requires a scoped execution and verifiable JTL source'
            return result

        # All projections must use the same input, even while persistence yields.
        fact = fact.model_copy(deep=True)
        structured_analysis = structured_analysis.model_copy(deep=True)

        # 1. Always archive the deterministic fact sheet first
        try:
            result["fact_archived"] = await fact_ledger.save(fact)
        except Exception as e:
            logger.error(f"Failed to archive fact sheet for {fact.execution_id}: {e}")
        if not result['fact_archived']:
            result['rejected_reason'] = 'Fact persistence failed; derived knowledge was not archived'
            result['errors'].append(result['rejected_reason'])
            return result

        # Missing model metadata is allowed; explicit conflicts cannot be relabeled.
        for field in ('execution_id', 'module', 'version'):
            supplied = getattr(structured_analysis, field)
            if supplied and supplied != getattr(fact, field):
                result['rejected_reason'] = f'Analysis {field} conflicts with the fact source'
                result['errors'].append(result['rejected_reason'])
                return result

        # 2. Validate numeric claims in the combined analysis text
        analysis_text = self._structured_analysis_to_text(structured_analysis)
        validation = fact_validator.validate(analysis_text, fact, extra_values)
        result["validation"] = validation

        # 3. Archive hypotheses (interpretations) regardless of number validation,
        #    because hypotheses are explicitly untested and carry their own confidence.
        hypotheses = structured_analysis.hypotheses
        if not hypotheses:
            # Fallback: try lightweight extraction from text
            try:
                hypotheses = hypothesis_extractor.extract(
                    analysis_text,
                    execution_id=fact.execution_id,
                    module=fact.module,
                    version=fact.version,
                )
            except Exception as e:
                logger.warning(f"Hypothesis extraction failed: {e}")

        for hyp in hypotheses:
            hyp.source_execution_id = fact.execution_id
            hyp.module = fact.module or hyp.module
            hyp.version = fact.version or hyp.version
            hyp.test_type = fact.test_type or hyp.test_type
            hyp.status = HypothesisStatus.UNTESTED
            hyp.verified_by_execution_id = None
            hyp.refuted_by_execution_id = None
            hyp.verification_notes = None
            hyp.hypothesis_id = hashlib.sha256(json.dumps(
                [fact.execution_id, hyp.category.value, hyp.statement], ensure_ascii=False).encode()).hexdigest()
            try:
                hid = await hypothesis_ledger.add(hyp, if_absent=True)
                result["hypotheses_archived"].append(hid)
            except Exception as e:
                logger.error(f"Failed to archive hypothesis {hyp.hypothesis_id}: {e}")
                result['errors'].append(f'Hypothesis persistence failed: {hyp.hypothesis_id}')

        # 4. Archive free-text conclusion ONLY if numeric claims are valid
        if not validation.is_valid:
            result["rejected_reason"] = (
                f"Analysis rejected from free-text archive due to numeric mismatches: "
                f"{validation.mismatches}"
            )
            logger.warning(result["rejected_reason"])
            return result

        try:
            result["conclusion_archived"] = self.archive_analysis_conclusion(
                analysis_text=analysis_text,
                module=fact.module or "unknown",
                version=fact.version or "unknown",
                test_type=fact.test_type or "",
                execution_id=fact.execution_id,
                _numeric_validated=True,
                _context_scope=fact.context_scope,
            )
            if result['conclusion_archived'] is None and self._is_substantial(analysis_text):
                result['errors'].append('Conclusion persistence failed')
        except Exception as e:
            logger.error('Failed to archive conclusion for %s: %s', fact.execution_id, e)
            result['errors'].append('Conclusion persistence failed')

        # 5. Also archive raw report projection
        if fact.raw_summary:
            try:
                report_data = fact.raw_summary.model_dump()
                result['raw_report_archived'] = self.archive_raw_report(
                    report_data=report_data,
                    module=fact.module or "unknown",
                    version=fact.version or "unknown",
                    execution_id=fact.execution_id,
                    _context_scope=fact.context_scope,
                )
                # None can be an intentional quality skip, not just a failed write.
                if (result['raw_report_archived'] is None and report_data.get('total_requests', 0) >= 10
                        and self._is_substantial(self._summarize_report(report_data))):
                    result['errors'].append('Raw report persistence failed')
            except Exception as e:
                logger.error('Failed to archive raw report for %s: %s', fact.execution_id, e)
                result['errors'].append('Raw report persistence failed')

        return result

    @staticmethod
    def _structured_analysis_to_text(analysis: StructuredAnalysis) -> str:
        """Convert structured analysis back to text for validation/archival."""
        parts = []
        if analysis.overall_assessment:
            parts.append(f"整体评估：{analysis.overall_assessment}")
        for stmt in analysis.metric_statements:
            parts.append(stmt)
        for rec in analysis.recommendations:
            parts.append(f"建议：{rec}")
        for hyp in analysis.hypotheses:
            parts.append(f"假设：{hyp.statement}")
        return "\n".join(parts)

    # === Legacy Archive Methods (kept for backward compatibility) ===

    def archive_raw_report(
        self,
        report_data: dict,
        module: str,
        version: str,
        execution_id: Optional[str] = None,
        plan_id: Optional[str] = None,
        *, _context_scope: Optional[str] = None,
    ) -> Optional[str]:
        """Archive a raw test report summary."""
        total = report_data.get("total_requests", 0)
        if total < 10:
            return None

        avg_rt = report_data.get("avg_response_time", 0)
        error_rate = report_data.get("error_rate", 0)
        throughput = report_data.get("throughput", 0)

        title = f"{module} {version} 测试报告 — TPS:{throughput:.1f} RT:{avg_rt:.0f}ms ER:{error_rate:.1f}%"

        return self._upsert(
            doc_type="raw_report",
            module=module,
            version=version,
            title=title,
            content=self._summarize_report(report_data),
            keywords=self._extract_report_keywords(report_data),
            source_execution_id=execution_id,
            source_plan_id=plan_id,
            _context_scope=_context_scope,
            metadata={
                "full_report": report_data,
                "jtl_is_source_of_truth": True,
            },
        )

    def archive_analysis_conclusion(
        self,
        analysis_text: str,
        module: str,
        version: str,
        test_type: str = "",
        execution_id: Optional[str] = None,
        *, _numeric_validated: bool = False,
        _context_scope: Optional[str] = None,
    ) -> Optional[str]:
        """Archive a formal analysis conclusion.

        NOTE: In the anti-pollution flow, this is only called after FactValidator passes.
        """
        if not self._is_substantial(analysis_text):
            return None

        title = f"{module} {version} 性能分析结论"
        if test_type:
            title += f" ({test_type})"

        return self._upsert(
            doc_type="analysis_conclusion",
            module=module,
            version=version,
            title=title,
            content=analysis_text,
            keywords=self._extract_text_keywords(analysis_text),
            source_execution_id=execution_id,
            _context_scope=_context_scope,
            metadata={
                "test_type": test_type,
                "validated": _numeric_validated,
                "validation_scope": "recognized_numeric_claims" if _numeric_validated else "unverified_manual_archive",
            },
        )

    def archive_hypothesis_document(
        self,
        hypothesis: Hypothesis,
    ) -> Optional[str]:
        """Archive a hypothesis also into LanceDB for RAG vector search."""
        if len(hypothesis.statement.strip()) < 10:
            return None

        title = f"[{hypothesis.status.value}] {hypothesis.module} {hypothesis.version or ''} {hypothesis.category.value}"
        content = (
            f"结论：{hypothesis.statement}\n"
            f"置信度：{hypothesis.confidence:.0%}\n"
            f"依赖指标：{', '.join(hypothesis.supporting_facts) or '无'}\n"
        )
        if hypothesis.testable_prediction:
            content += f"可验证预测：{hypothesis.testable_prediction}\n"
        if hypothesis.falsification_design:
            content += f"证伪实验设计：{hypothesis.falsification_design}\n"

        return self._upsert(
            doc_type="hypothesis",
            module=hypothesis.module,
            version=hypothesis.version or "unknown",
            title=title,
            content=content,
            keywords=[hypothesis.category.value, hypothesis.status.value, hypothesis.module],
            source_execution_id=hypothesis.source_execution_id,
            metadata=hypothesis.model_dump(),
        )

    def archive_defect_retrospective(
        self,
        defect_title: str,
        root_cause: str,
        module: str,
        version: str,
        severity: str = "medium",
        execution_id: Optional[str] = None,
    ) -> Optional[str]:
        """Archive a defect root-cause analysis."""
        if len(root_cause.strip()) < 50:
            return None

        return self._upsert(
            doc_type="defect_retrospective",
            module=module,
            version=version,
            title=defect_title,
            content=root_cause,
            keywords=self._extract_text_keywords(defect_title + " " + root_cause),
            source_execution_id=execution_id,
            metadata={"severity": severity},
        )

    def archive_known_issue(
        self,
        title: str,
        description: str,
        module: str,
        version: str = "",
        workaround: str = "",
    ) -> Optional[str]:
        """Archive a high-frequency known issue."""
        if len(description.strip()) < 30:
            return None

        content = description
        if workaround:
            content += f"\n\n### 解决方案\n{workaround}"

        return self._upsert(
            doc_type="known_issue",
            module=module,
            version=version or "ongoing",
            title=title,
            content=content,
            keywords=self._extract_text_keywords(title + " " + description),
            metadata={"has_workaround": bool(workaround)},
        )

    def archive_module_standard(
        self,
        module: str,
        version: str,
        standard_data: dict,
    ) -> Optional[str]:
        """Archive a module performance standard/baseline."""
        content = self._dict_to_text(standard_data)
        if len(content) < 30:
            return None

        title = f"{module} {version} 性能基准标准"

        return self._upsert(
            doc_type="module_standard",
            module=module,
            version=version,
            title=title,
            content=content,
            keywords=["性能基准", "baseline", module, "QPS标准", "响应时间标准"],
            metadata={"standard_data": standard_data},
        )

    # === Quality checks ===

    def _is_substantial(self, text: str) -> bool:
        """Check if text has enough substance to archive."""
        if len(text.strip()) < 100:
            return False

        substance_indicators = [
            r'\d+ms', r'\d+%', r'\d+req/s', r'\d+ TPS',
            r'瓶颈', r'根因', r'优化', r'建议', r'修复',
            r'bottleneck', r'root cause', r'optimize', r'fix', r'improve',
            r'连接池', r'线程池', r'GC', r'CPU', r'内存',
            r'增加.*\d+', r'调整.*\d+', r'升级',
        ]

        for pattern in substance_indicators:
            if re.search(pattern, text, re.IGNORECASE):
                return True

        return False

    # === Helpers ===

    def _upsert(
        self,
        doc_type: str,
        module: str,
        version: str,
        title: str,
        content: str,
        keywords: list[str],
        source_execution_id: Optional[str] = None,
        source_plan_id: Optional[str] = None,
        metadata: Optional[dict] = None,
        *, _context_scope: Optional[str] = None,
    ) -> Optional[str]:
        """Insert or update a document. Returns doc ID or None if skipped."""
        if doc_type not in ALLOWED_DOC_TYPES:
            return None

        if not self._is_substantial(content):
            return None

        from app.services.ai.memory.provenance import execution_scope
        scope = _context_scope if _context_scope is not None else execution_scope(source_execution_id)
        identity = [doc_type, module, version, scope, source_execution_id, source_plan_id]
        if not source_execution_id:
            identity.append(title)  # Independent manual knowledge must not collapse by module/type.
        doc_id = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()
        existing = self.store.get(doc_id)
        if existing:
            updates = dict(title=title, content=content, keywords=keywords,
                           metadata={**existing.metadata, **(metadata or {})})
            if all(getattr(existing, key) == value for key, value in updates.items()):
                return doc_id
            saved = self.store.update(doc_id, **updates)
            return saved.id if saved else None

        doc = RAGDocument(
            id=doc_id,
            doc_type=doc_type,
            module=module,
            version=version,
            title=title,
            content=content,
            keywords=keywords,
            source_execution_id=source_execution_id,
            source_plan_id=source_plan_id,
            metadata=metadata or {},
            context_scope=scope,
        )
        return self.store.add(doc)

    def _summarize_report(self, report_data: dict) -> str:
        """Create a concise text summary of a raw report for storage."""
        lines = []
        lines.append(f"总请求数: {report_data.get('total_requests', 0)}")
        lines.append(f"平均响应时间: {report_data.get('avg_response_time', 0)}ms")
        lines.append(f"最大响应时间: {report_data.get('max_response_time', 0)}ms")
        lines.append(f"错误率: {report_data.get('error_rate', 0)}%")
        lines.append(f"吞吐量: {report_data.get('throughput', 0)} req/s")

        percentiles = report_data.get("percentiles") or {}
        if percentiles:
            lines.append(f"分位数: p50={percentiles.get('p50', 'N/A')}ms, "
                        f"p95={percentiles.get('p95', 'N/A')}ms, "
                        f"p99={percentiles.get('p99', 'N/A')}ms")

        lines.append("\n注：此报告为 JTL 原始数据的投影，如存疑请以 JTL 文件重新计算。")
        return "\n".join(lines)

    def _extract_report_keywords(self, report_data: dict) -> list[str]:
        """Extract keywords from report data."""
        kws = []
        if report_data.get("error_rate", 0) > 5:
            kws.append("高错误率")
        if report_data.get("error_rate", 0) > 1:
            kws.append("错误率异常")
        p99 = (report_data.get("percentiles") or {}).get("p99", 0)
        if p99 and p99 > 1000:
            kws.append("p99高延迟")
        trend = report_data.get("performance_trend") or {}
        if trend.get("trend_direction") == "degrading":
            kws.append("性能恶化")
        for err in (report_data.get("error_details") or [])[:3]:
            kws.append(err.get("error_type", ""))
        return [k for k in kws if k]

    def _extract_text_keywords(self, text: str) -> list[str]:
        """Extract keywords from free text."""
        keywords = []
        tech_terms = [
            "连接池", "线程池", "数据库", "缓存", "GC", "内存泄漏", "死锁",
            "CPU", "内存", "网络", "磁盘", "IO", "队列", "锁竞争", "限流",
            "bottleneck", "connection pool", "thread pool", "deadlock",
            "memory leak", "lock contention", "GC", "database",
        ]
        text_lower = text.lower()
        for term in tech_terms:
            if term.lower() in text_lower:
                keywords.append(term)
        return keywords[:10]

    @staticmethod
    def _dict_to_text(data: dict, indent: int = 0) -> str:
        """Convert a dict to readable text."""
        lines = []
        for k, v in data.items():
            prefix = "  " * indent
            if isinstance(v, dict):
                lines.append(f"{prefix}{k}:")
                lines.append(RAGArchiver._dict_to_text(v, indent + 1))
            else:
                lines.append(f"{prefix}{k}: {v}")
        return "\n".join(lines)


# Module singleton
rag_archiver = RAGArchiver()

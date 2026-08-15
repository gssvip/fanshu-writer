"""
轻量语义检索（借鉴 PlotPilot 向量召回，纯 Python TF-IDF 实现）。

设计目标：
  - 零外部依赖（不依赖 numpy / sklearn / 向量数据库）
  - 纯 Python 实现 TF-IDF + 余弦相似度
  - 解决长篇创作"百万字失忆"：精准召回语义相关历史章节
  - 作为现有 _recall_related_chapters（字符匹配）的语义增强补充

使用方式：
  from semantic_retriever import SemanticRetriever
  retriever = SemanticRetriever()
  retriever.index_chapters(chapters)  # 索引历史章节
  results = retriever.search(query_text, top_k=5)  # 语义检索
"""
import re
import math
from typing import List, Dict, Tuple, Optional
from collections import Counter, defaultdict


# 中文停用词（高频无意义词）
_STOP_WORDS = {
    '的', '了', '是', '在', '有', '和', '与', '就', '不', '都', '一', '这', '那', '他', '她', '它',
    '我', '你', '们', '上', '下', '中', '里', '到', '为', '之', '也', '而', '或', '但', '若',
    '其', '此', '于', '以', '把', '被', '让', '给', '向', '从', '对', '着', '过', '地', '得',
    '着', '来', '去', '又', '只', '才', '便', '即', '已', '将', '该', '某', '各', '每', '另',
    '什么', '怎么', '为什么', '如何', '可以', '可能', '应该', '或者', '因为', '所以', '如果',
    '虽然', '但是', '然而', '于是', '然后', '现在', '当时', '后来', '之前', '之后',
}


def _tokenize(text: str) -> List[str]:
    """中文分词：按字切分 + 2字词组合（bigram），过滤停用词和标点。
    简化方案：不依赖 jieba，用 unigram + bigram 捕捉局部语义。"""
    if not text:
        return []
    cleaned = re.sub(r'[\s\W_]+', '', text)
    if not cleaned:
        return []
    tokens = []
    for ch in cleaned:
        if ch not in _STOP_WORDS:
            tokens.append(ch)
    for i in range(len(cleaned) - 1):
        bigram = cleaned[i:i+2]
        if bigram not in _STOP_WORDS and not any(c in _STOP_WORDS for c in bigram):
            tokens.append(bigram)
    return tokens


# ---- 全局语料频率（跨书共享，用于 IDF 平滑；首次使用时 lazy 构造） ----
_GLOBAL_DF: Optional[Counter] = None
_GLOBAL_N: int = 0


def _ensure_global_df_corpus(examples: Optional[List[str]] = None) -> None:
    """首次调用时构造一个通用的"全局语料背景频率"。
    - 小说常见主题词、动词、情感词等
    目的：新书本数少时（N 小 → IDF 被放大），把 corpus-level IDF 作为下限，
    防止出现某词仅出现在 1 章里被赋予过大权重。"""
    global _GLOBAL_DF, _GLOBAL_N
    if _GLOBAL_DF is not None:
        return
    corpus = [
        '主角 武功 心法 真气 修炼 突破 招式 对手 门派 江湖 秘籍',
        '皇帝 朝堂 大臣 将军 军队 征战 城池 百姓 粮草 赋税',
        '父亲 母亲 师父 师兄 师姐 弟子 朋友 恋人 兄弟 仇人',
        '城市 村庄 山脉 河流 森林 沙漠 海岛 秘境 遗迹 宫殿',
        '白天 夜晚 清晨 黄昏 春季 秋季 冬季 夏季 风雪 雷雨',
        '愤怒 悲伤 喜悦 恐惧 痛苦 嫉妒 懊悔 兴奋 疑惑 犹豫',
        '秘密 真相 阴谋 计划 背叛 合作 冒险 逃亡 回归 成长',
        '战斗 逃跑 谈判 调查 埋伏 突袭 胜利 失败 计谋 陷阱',
        '金钱 宝物 丹药 武器 书籍 信件 信物 钥匙 地图 药草',
        '时间 空间 命运 因果 轮回 重生 穿越 系统 任务 奖励',
    ]
    if examples:
        corpus.extend(examples)
    _GLOBAL_DF = Counter()
    _GLOBAL_N = len(corpus)
    for doc in corpus:
        tokens = set(_tokenize(doc))
        for t in tokens:
            _GLOBAL_DF[t] += 1


class SemanticRetriever:
    """轻量 TF-IDF 语义检索器。

    索引策略（P2 升级）：
    - 每章不是 1 个文档，而是拆成多个 chunk（默认 2000 汉字 / chunk，带 200 字 overlap）
    - 每个 chunk 独立计算 TF-IDF 向量
    - 章节层面的向量 = 该章所有 chunk 向量的 mean-vector（权重归一化后按位平均）
    - 搜索时：先在 chunk 级别找相似度，再按"取该章所有 chunk 的 top-1 相似度"作为章节得分
      （这样能确保某章里只要有一小段与 query 匹配，就会被召回，
       同时整章向量 mean 做备份，整体相关性也不会丢）
    """

    def __init__(self, chunk_size: int = 2000, chunk_overlap: int = 200,
                 use_full_text: bool = True):
        """
        :param chunk_size: 每块字符数（汉字，含标点）
        :param chunk_overlap: 相邻块重叠字符数，避免语义切分点丢失
        :param use_full_text: 是否启用"整章 chunk 化"；False 时降级为原来的"标题+摘要"模式
        """
        # 章节元数据
        self._docs: List[Dict] = []
        # 每章对应 chunk 在 self._chunks 中的起止范围
        self._chapter_ranges: List[Tuple[int, int]] = []
        # 所有 chunk（跨章扁平列表）
        self._chunks: List[Dict] = []  # {chapter_idx, offset, text, tokens, tf_norm_sq?}
        # 章节级别 mean 向量（归一化）
        self._chapter_mean_vectors: List[Dict[str, float]] = []

        # TF-IDF 底层数据
        self._df: Counter = Counter()
        self._idf: Dict[str, float] = {}
        self._chunk_tfidf_vectors: List[Dict[str, float]] = []
        self._indexed = False

        self.chunk_size = max(200, int(chunk_size))
        self.chunk_overlap = max(0, min(self.chunk_size // 4, int(chunk_overlap)))
        self.use_full_text = bool(use_full_text)

    # ------------------------------------------------------------------
    # chunk 切分
    # ------------------------------------------------------------------
    def _split_chunks(self, text: str) -> List[str]:
        """把整章正文切分成若干重叠块。短于 chunk_size 的直接返回一块。"""
        if not text:
            return []
        if len(text) <= self.chunk_size:
            return [text]
        chunks: List[str] = []
        start = 0
        n = len(text)
        step = self.chunk_size - self.chunk_overlap
        if step <= 0:
            step = self.chunk_size
        while start < n:
            end = min(start + self.chunk_size, n)
            chunks.append(text[start:end])
            if end >= n:
                break
            start += step
        # 保证最后一块至少有 overlap 的一半长度，否则与前一块合并
        if len(chunks) >= 2 and len(chunks[-1]) < (self.chunk_overlap // 2 + 50):
            tail = chunks.pop()
            chunks[-1] = chunks[-1] + tail[-max(0, len(tail)):]
        return chunks

    # ------------------------------------------------------------------
    # 索引构建
    # ------------------------------------------------------------------
    def index_chapters(self, chapters: List[Dict]):
        """索引历史章节。
        每章字段：{'chapter_num', 'title', 'summary', 'content'}（content 可选）
        use_full_text=True 时优先用 content；否则回退到 summary。
        """
        self._docs = []
        self._chapter_ranges = []
        self._chunks = []
        self._chapter_mean_vectors = []
        self._df = Counter()
        self._chunk_tfidf_vectors = []
        chapter_idx = -1

        # 用于构造全局 IDF 的样本：把本书所有 summary 当例子
        _ensure_global_df_corpus([(c.get('summary') or '') for c in chapters if c.get('summary')])

        for ch in chapters:
            chapter_idx += 1
            title = (ch.get('title') or '').strip()
            summary = (ch.get('summary') or '').strip()
            content = (ch.get('content') or '').strip() if self.use_full_text else ''

            # 决定本章参与索引的文本段（chunk 列表）
            source_texts: List[str] = []
            if content and len(content) >= 50:
                # 整章 chunk 化；每块前缀都带上 title + summary 的浓缩，保证主题信号
                prefix = f'{title} {summary[:200]} '
                for raw_chunk in self._split_chunks(content):
                    source_texts.append(prefix + raw_chunk)
            elif summary and len(summary) >= 10:
                # 回退：标题+摘要（旧模式）
                source_texts = [f'{title} {summary}']
            else:
                # 内容太少：跳过
                continue

            self._docs.append({
                'chapter_num': ch.get('chapter_num', 0),
                'title': title,
                # 召回给 LLM 看的摘要：优先用 summary，没有则从 content 截一段
                'summary': (summary[:500] if summary else (content[:500] if content else '')),
            })

            start_cidx = len(self._chunks)
            # 为本章每个 chunk 做 tokens + tf，并累计 df
            chapter_tf_list: List[Counter] = []
            for s in source_texts:
                tokens = _tokenize(s)
                if not tokens:
                    continue
                tf = Counter(tokens)
                chapter_tf_list.append(tf)
                self._chunks.append({
                    'chapter_idx': chapter_idx,
                    'text': s[:200],  # 调试留痕，不参与向量
                })
                for token in tf:
                    self._df[token] += 1
            end_cidx = len(self._chunks)
            self._chapter_ranges.append((start_cidx, end_cidx))
            # 先占位，等 idf 算完再统一填
            self._chapter_mean_vectors.append({})

        # ---- 计算 IDF（融合全局 IDF 作为下限，解决新书样本量小的问题） ----
        N = len(self._chunks)
        if N == 0:
            self._indexed = False
            return

        global_df = _GLOBAL_DF or Counter()
        global_n = _GLOBAL_N or 1
        for token, df in self._df.items():
            # 本书 df + 全局 df 作为平滑；IDF 取两者的较小者（更保守）
            blended_df = df + global_df.get(token, 0)
            blended_N = N + global_n
            # 额外设一个 IDF 下限：log(2*blended_N / blended_N) ≈ 0.69，避免无限放大罕见词
            idf_raw = math.log(blended_N / (blended_df + 1)) + 1
            idf_min = math.log((blended_N) / (blended_N / 2 + 1)) + 1
            self._idf[token] = max(idf_min, idf_raw)

        # ---- 计算每个 chunk 的 TF-IDF 向量 ----
        for tf in chapter_tf_list:
            pass  # 占位，避免重复读变量；下面重算一遍

        # 重新按顺序遍历章节，拿到每个 chunk 的 tf：
        cursor = 0
        for chapter_idx in range(len(self._docs)):
            s, e = self._chapter_ranges[chapter_idx]
            mean_vec: Dict[str, float] = defaultdict(float)
            chunk_count = max(1, e - s)
            for cidx in range(s, e):
                # 因为 self._chunks 顺序与 chapter_tf_list 顺序一致，只是之前没保留下来，
                # 这里重算 tf 一次（索引是后台冷路径，可接受；比再存一份大数组省内存）
                chunk_text = self._chunks[cidx]['text']
                tf = Counter(_tokenize(chunk_text))
                vec: Dict[str, float] = {}
                total_tokens = sum(tf.values()) or 1
                for token, freq in tf.items():
                    tf_val = freq / total_tokens
                    vec[token] = tf_val * self._idf.get(token, 0)
                norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
                for k in vec:
                    vec[k] /= norm
                    # 累计到章节均值向量（先累加，最后除以 chunk_count）
                    mean_vec[k] += vec[k]
                self._chunk_tfidf_vectors.append(vec)
                cursor += 1
            # 章节均值向量：平均后再做 L2 归一化
            for k in mean_vec:
                mean_vec[k] /= chunk_count
            mean_norm = math.sqrt(sum(v * v for v in mean_vec.values())) or 1.0
            for k in mean_vec:
                mean_vec[k] /= mean_norm
            self._chapter_mean_vectors[chapter_idx] = dict(mean_vec)

        self._indexed = True

    # ------------------------------------------------------------------
    # 搜索
    # ------------------------------------------------------------------
    def search(self, query: str, top_k: int = 5, exclude_nums: Optional[set] = None) -> List[Dict]:
        """语义检索（P2 双路聚合）。

        打分策略：
        1) chunk-level best-hit：取该章所有 chunk 与 query 的最大相似度
        2) chapter-level mean：取该章 mean 向量与 query 的相似度
        3) 最终得分 = alpha * best_hit + (1 - alpha) * mean_sim  （alpha=0.75，偏向局部命中）
        """
        if not self._indexed or not query:
            return []
        query_tokens = _tokenize(query)
        if not query_tokens:
            return []
        # query TF-IDF + L2 归一化
        query_tf = Counter(query_tokens)
        query_vec: Dict[str, float] = {}
        total_tok = sum(query_tf.values()) or 1
        for token, freq in query_tf.items():
            tf_val = freq / total_tok
            query_vec[token] = tf_val * self._idf.get(token, 0)
        qnorm = math.sqrt(sum(v * v for v in query_vec.values())) or 1.0
        for k in query_vec:
            query_vec[k] /= qnorm

        def _cos(a: Dict[str, float], b: Dict[str, float]) -> float:
            if len(a) < len(b):
                return sum(v * b.get(k, 0.0) for k, v in a.items())
            return sum(v * a.get(k, 0.0) for k, v in b.items())

        # 1. chunk 级 best-hit 得分
        chunk_best: Dict[int, float] = defaultdict(float)
        for cidx, cvec in enumerate(self._chunk_tfidf_vectors):
            chapter_idx = self._chunks[cidx]['chapter_idx']
            if chapter_idx < 0 or chapter_idx >= len(self._docs):
                continue
            if exclude_nums and self._docs[chapter_idx]['chapter_num'] in exclude_nums:
                continue
            sim = _cos(query_vec, cvec)
            if sim > chunk_best[chapter_idx]:
                chunk_best[chapter_idx] = sim

        # 2. 聚合
        ALPHA = 0.75
        chapter_scores: List[Tuple[int, float, float, float]] = []
        for chapter_idx, doc in enumerate(self._docs):
            chapter_num = doc['chapter_num']
            if exclude_nums and chapter_num in exclude_nums:
                continue
            best_hit = chunk_best.get(chapter_idx, 0.0)
            mean_sim = _cos(query_vec, self._chapter_mean_vectors[chapter_idx])
            final = ALPHA * best_hit + (1.0 - ALPHA) * mean_sim
            if final > 0.01:
                chapter_scores.append((chapter_idx, final, best_hit, mean_sim))

        chapter_scores.sort(key=lambda x: -x[1])

        results = []
        for chapter_idx, final, best_hit, mean_sim in chapter_scores[:top_k]:
            doc = self._docs[chapter_idx]
            results.append({
                'chapter_num': doc['chapter_num'],
                'title': doc['title'],
                'summary': doc['summary'],
                'score': round(final, 3),
                'score_detail': {
                    'best_chunk_hit': round(best_hit, 3),
                    'chapter_mean_sim': round(mean_sim, 3),
                },
            })
        return results


# 模块级单例 + 缓存（按 book_id 缓存索引，避免重复构建）
_retriever_cache: Dict[str, Tuple[SemanticRetriever, int, int]] = {}  # {book_id: (retriever, 章节数, 内容hash或内容总字数)}


def _chapters_fingerprint(chapters: List[Dict]) -> int:
    """粗略指纹：章节数 + 每章 content 长度之和（content 为空则用 summary 长度）。
    当且仅当"新增章节"或"旧章内容长度发生变化"时触发索引重建。"""
    total = 0
    for c in chapters:
        total += len(c.get('content') or '') or len(c.get('summary') or '')
    return total


def recall_semantic_chapters(
    book_id: str,
    query: str,
    current_chapter_num: int,
    exclude_recent: int = 4,
    max_chapters: int = 5,
    chapters_provider=None,
) -> List[Dict]:
    """语义召回历史章节（供 app.py 调用）。
    - chapters_provider: 回调，返回 [{'chapter_num','title','summary','content'}] 列表
    - exclude_recent: 排除最近 N 章（避免与即时层重复）
    返回召回结果列表。"""
    if not query or not chapters_provider:
        return []
    try:
        chapters = chapters_provider()
        if not chapters:
            return []
        n_ch = len(chapters)
        fp = _chapters_fingerprint(chapters)
        cached = _retriever_cache.get(book_id)
        if cached and cached[1] == n_ch and cached[2] == fp:
            retriever = cached[0]
        else:
            retriever = SemanticRetriever(chunk_size=2000, chunk_overlap=200, use_full_text=True)
            retriever.index_chapters(chapters)
            _retriever_cache[book_id] = (retriever, n_ch, fp)
        exclude_nums = set(range(max(1, current_chapter_num - exclude_recent + 1), current_chapter_num + 1))
        return retriever.search(query, top_k=max_chapters, exclude_nums=exclude_nums)
    except Exception:
        return []

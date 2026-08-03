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
    # 去标点和空白
    cleaned = re.sub(r'[\s\W_]+', '', text)
    if not cleaned:
        return []
    tokens = []
    # unigram
    for ch in cleaned:
        if ch not in _STOP_WORDS:
            tokens.append(ch)
    # bigram（捕捉2字词）
    for i in range(len(cleaned) - 1):
        bigram = cleaned[i:i+2]
        if bigram not in _STOP_WORDS and not any(c in _STOP_WORDS for c in bigram):
            tokens.append(bigram)
    return tokens


class SemanticRetriever:
    """轻量 TF-IDF 语义检索器。
    索引章节摘要，支持按查询文本召回语义最相关的章节。"""

    def __init__(self):
        self._docs: List[Dict] = []  # [{'chapter_num', 'title', 'summary', 'tokens'}]
        self._df: Counter = Counter()  # 文档频率：每个 token 出现在多少文档
        self._tf: List[Counter] = []  # 每篇文档的词频
        self._idf: Dict[str, float] = {}
        self._tfidf_vectors: List[Dict[str, float]] = []
        self._indexed = False

    def index_chapters(self, chapters: List[Dict]):
        """索引历史章节。
        chapters: [{'chapter_num', 'title', 'summary'}]
        幂等：重复调用会重建索引。"""
        self._docs = []
        self._df = Counter()
        self._tf = []
        self._tfidf_vectors = []
        for ch in chapters:
            summary = (ch.get('summary') or '').strip()
            if len(summary) < 10:
                continue
            # 拼接标题+摘要作为索引文本（标题含关键信息）
            text = (ch.get('title') or '') + ' ' + summary
            tokens = _tokenize(text)
            if not tokens:
                continue
            tf = Counter(tokens)
            self._docs.append({
                'chapter_num': ch.get('chapter_num', 0),
                'title': ch.get('title') or '',
                'summary': summary[:300],  # 索引保留 300 字，召回时截断
            })
            self._tf.append(tf)
            for token in tf:
                self._df[token] += 1
        # 计算 IDF
        N = len(self._docs)
        if N == 0:
            self._indexed = False
            return
        for token, df in self._df.items():
            # IDF = log(N / (df + 1))，加1平滑
            self._idf[token] = math.log(N / (df + 1)) + 1
        # 计算每篇文档的 TF-IDF 向量
        for tf in self._tf:
            vec = {}
            for token, freq in tf.items():
                tf_val = freq / (sum(tf.values()) or 1)  # 归一化 TF
                vec[token] = tf_val * self._idf.get(token, 0)
            # L2 归一化
            norm = math.sqrt(sum(v * v for v in vec.values())) or 1
            for k in vec:
                vec[k] /= norm
            self._tfidf_vectors.append(vec)
        self._indexed = True

    def search(self, query: str, top_k: int = 5, exclude_nums: Optional[set] = None) -> List[Dict]:
        """语义检索：返回与 query 最相关的 top_k 章节。
        exclude_nums: 排除的章号集合（如最近4章，避免与即时层重复）。"""
        if not self._indexed or not query:
            return []
        query_tokens = _tokenize(query)
        if not query_tokens:
            return []
        # 计算 query 的 TF-IDF 向量
        query_tf = Counter(query_tokens)
        query_vec = {}
        for token, freq in query_tf.items():
            tf_val = freq / (sum(query_tf.values()) or 1)
            query_vec[token] = tf_val * self._idf.get(token, 0)
        # L2 归一化
        norm = math.sqrt(sum(v * v for v in query_vec.values())) or 1
        for k in query_vec:
            query_vec[k] /= norm
        # 余弦相似度
        scores: List[Tuple[int, float]] = []
        for i, doc_vec in enumerate(self._tfidf_vectors):
            chapter_num = self._docs[i]['chapter_num']
            if exclude_nums and chapter_num in exclude_nums:
                continue
            # 点积（只计算共有的 token）
            dot = 0.0
            # 遍历较短的向量
            if len(query_vec) < len(doc_vec):
                for token, q_val in query_vec.items():
                    if token in doc_vec:
                        dot += q_val * doc_vec[token]
            else:
                for token, d_val in doc_vec.items():
                    if token in query_vec:
                        dot += d_val * query_vec[token]
            if dot > 0.01:  # 过滤几乎不相关的
                scores.append((i, dot))
        # 排序取 top_k
        scores.sort(key=lambda x: -x[1])
        results = []
        for i, score in scores[:top_k]:
            doc = self._docs[i]
            results.append({
                'chapter_num': doc['chapter_num'],
                'title': doc['title'],
                'summary': doc['summary'],
                'score': round(score, 3),
            })
        return results


# 模块级单例 + 缓存（按 book_id 缓存索引，避免重复构建）
_retriever_cache: Dict[str, Tuple[SemanticRetriever, int]] = {}  # {book_id: (retriever, 章节计数)}


def recall_semantic_chapters(
    book_id: str,
    query: str,
    current_chapter_num: int,
    exclude_recent: int = 4,
    max_chapters: int = 5,
    chapters_provider=None,
) -> List[Dict]:
    """语义召回历史章节（供 app.py 调用）。
    - chapters_provider: 回调函数，返回 [{'chapter_num','title','summary'}] 列表
    - exclude_recent: 排除最近 N 章（避免与即时层重复）
    返回召回结果列表。"""
    if not query or not chapters_provider:
        return []
    try:
        chapters = chapters_provider()
        if not chapters:
            return []
        # 检查缓存是否可复用（章节数未变）
        cached = _retriever_cache.get(book_id)
        if cached and cached[1] == len(chapters):
            retriever = cached[0]
        else:
            retriever = SemanticRetriever()
            retriever.index_chapters(chapters)
            _retriever_cache[book_id] = (retriever, len(chapters))
        # 排除最近 N 章
        exclude_nums = set(range(max(1, current_chapter_num - exclude_recent + 1), current_chapter_num + 1))
        return retriever.search(query, top_k=max_chapters, exclude_nums=exclude_nums)
    except Exception:
        return []

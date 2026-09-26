import { useState, useContext } from 'react';
import { api } from '../../api';
import { AuthContext } from '../../App';
import type { ReviewResult } from '../../types';

const REVIEW_LABELS: Record<string, string> = {
  opening_hook: '开篇钩子', character_motivation: '人物动机', pacing_rhythm: '节奏控制',
  chapter_ending: '章尾钩子', dialogue_quality: '对白质量',
  world_consistency: '设定一致性', commercial_potential: '商业潜力',
};

export default function ReviewTab({ selectedBookId }: { selectedBookId: string }) {
  const { requireAuth } = useContext(AuthContext);
  const [loading, setLoading] = useState(false);
  const [reviewResult, setReviewResult] = useState<ReviewResult | null>(null);
  const [reviewError, setReviewError] = useState('');

  async function handleReview() {
    if (!selectedBookId) return;
    const ok = await requireAuth();
    if (!ok) return;
    setLoading(true);
    setReviewError('');
    setReviewResult(null);
    try { const r = await api.reviewBook(selectedBookId); setReviewResult(r); }
    catch (e: any) { setReviewError(e.message || '审稿失败'); }
    setLoading(false);
  }

  return (
    <div className="tool-panel">
      <h3>🔍 AI 责编审稿</h3>
      <button className="btn-primary" onClick={handleReview} disabled={!selectedBookId || loading}>
        {loading ? '审稿中...' : '开始审稿'}
      </button>
      {reviewError && <div className="error-msg">{reviewError}</div>}
      {reviewResult && (
        <div className="review-result">
          <div className="review-score-header">
            <div className="review-total-score">{reviewResult.total_score}</div>
            <div className="review-grade">
              <span className={`grade-badge grade-${reviewResult.grade?.toLowerCase()}`}>{reviewResult.grade}级</span>
              <span>{reviewResult.platform_fit}</span>
            </div>
          </div>
          <div className="review-scores-grid">
            {Object.entries(reviewResult.scores).map(([key, score]) => (
              <div key={key} className="review-score-item">
                <div className="review-score-label">{REVIEW_LABELS[key] || key}</div>
                <div className="review-score-bar"><div className="review-score-fill" style={{ width: `${score}%` }} /></div>
                <div className="review-score-value">{score}</div>
              </div>
            ))}
          </div>
          <div className="review-section"><h4>优点</h4><ul>{reviewResult.strengths?.map((s, i) => <li key={i}>{s}</li>)}</ul></div>
          <div className="review-section"><h4>改进建议</h4><ul>{reviewResult.weaknesses?.map((w, i) => <li key={i}>{w}</li>)}</ul></div>
          <div className="review-section"><h4>具体修改方案</h4><ul>{reviewResult.specific_suggestions?.map((s, i) => <li key={i}>{s}</li>)}</ul></div>
        </div>
      )}
    </div>
  );
}